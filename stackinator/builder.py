import json
import os
import pathlib
import platform
import shutil
import stat
import subprocess
import sys
from datetime import datetime

import jinja2
import yaml

from . import VERSION, cache, root_logger, spack_util


def install(src, dst, *, ignore=None, symlinks=False):
    """Call shutil.copytree or shutil.copy2. copy2 is used if `src` is not a directory.
    Afterwards run the equivalent of chmod a+rX dst."""

    def apply_permissions_recursive(directory):
        """Apply permissions recursively to an entire directory."""

        def set_permissions(path):
            """Set permissions for a given path based on chmod a+rX equivalent."""
            mode = os.stat(path).st_mode
            # Always give read permissions for user, group, and others.
            new_mode = mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
            # If it's a directory or execute bit is set for owner or group,
            # set execute bit for all.
            if stat.S_ISDIR(mode) or mode & (stat.S_IXUSR | stat.S_IXGRP):
                new_mode |= stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            os.chmod(path, new_mode)

        set_permissions(directory)
        for dirpath, dirnames, filenames in os.walk(directory):
            for dirname in dirnames:
                set_permissions(os.path.join(dirpath, dirname))
            for filename in filenames:
                set_permissions(os.path.join(dirpath, filename))

    if stat.S_ISDIR(os.stat(src).st_mode):
        shutil.copytree(
            src,
            dst,
            ignore=ignore,
            symlinks=symlinks,
        )
    else:
        shutil.copy2(src, dst, follow_symlinks=symlinks)
    # set permissions
    apply_permissions_recursive(dst)


class Builder:
    def __init__(self, args):
        self._logger = root_logger
        path = pathlib.Path(args.build)
        if not path.is_absolute():
            path = pathlib.Path.cwd() / path

        # check that if the path exists that it is not a file
        if path.exists():
            if not path.is_dir():
                raise IOError("build path is not a directory")

        parts = path.parts

        # the build path can't be root
        if len(parts) == 1:
            raise IOError("build path can't be root '/'")

        # the build path can't be in /tmp because the build step rebinds /tmp.
        if parts[1] == "tmp":
            raise IOError("build path can't be in '/tmp'")

        # the build path can't be in $HOME because the build step rebinds $HOME
        # NOTE that this would be much easier to determine with PosixPath.is_relative_to
        # introduced in Python 3.9.
        home_parts = pathlib.Path.home().parts
        if (len(home_parts) <= len(parts)) and (home_parts == parts[: len(home_parts)]):
            raise IOError("build path can't be in '$HOME' or '~'")
        # if path.is_relative_to(pathlib.Path.home()):
        # raise IOError("build path can't be in '$HOME' or '~'")

        self.path = path
        self.root = pathlib.Path(__file__).parent.resolve()

    @property
    def configuration_meta(self):
        """Meta data about the configuration and build"""
        return self._configuration_meta

    @configuration_meta.setter
    def configuration_meta(self, recipe):
        # generate configuration meta data
        meta = {}
        meta["time"] = datetime.now().strftime("%Y%m%d %H:%M:%S")
        host_data = platform.uname()
        meta["host"] = {
            "machine": host_data.machine,
            "node": host_data.node,
            "processor": host_data.processor,
            "release": host_data.release,
            "system": host_data.system,
            "version": host_data.version,
        }
        meta["cluster"] = os.getenv("CLUSTER_NAME", default="unknown")
        meta["stackinator"] = {
            "version": VERSION,
            "args": sys.argv,
            "python": sys.executable,
        }
        meta["mount"] = str(recipe.mount)
        meta["spack"] = recipe.config["spack"]
        self._configuration_meta = meta

    @property
    def environment_meta(self):
        """The meta data file that describes the environments"""
        return self._environment_meta

    @environment_meta.setter
    def environment_meta(self, recipe):
        """
        The output that we want to generate looks like the following,
        Which should correspond directly to the environment_view_meta provided
        by the recipe.

        {
          name: "prgenv-gnu",
          description: "useful programming tools",
          mount: "/user-environment"
          modules: {
              "root": /user-environment/modules,
          },
          views: {
            "default": {
              "root": /user-environment/env/default,
              "activate": /user-environment/env/default/activate.sh,
              "description": "simple devolpment env: compilers, MPI, python, cmake."
            },
            "tools": {
              "root": /user-environment/env/tools,
              "activate": /user-environment/env/tools/activate.sh,
              "description": "handy tools"
            }
          }
        }
        """
        conf = recipe.config
        meta = {}
        meta["name"] = conf["name"]
        meta["description"] = conf["description"]
        meta["views"] = recipe.environment_view_meta
        meta["mount"] = str(recipe.mount)
        modules = None
        if conf["modules"]:
            modules = {"root": str(recipe.mount / "modules")}
        meta["modules"] = modules
        self._environment_meta = meta

    def generate(self, recipe):
        # make the paths, in case bwrap is not used, directly write to recipe.mount
        store_path = self.path / "store" if not recipe.no_bwrap else pathlib.Path(recipe.mount)
        tmp_path = self.path / "tmp"

        self.path.mkdir(exist_ok=True, parents=True)
        store_path.mkdir(exist_ok=True)
        tmp_path.mkdir(exist_ok=True)

        # check out the version of spack
        spack_version = recipe.spack_version
        self._logger.debug(f"spack version for templates: {spack_version}")
        spack = recipe.config["spack"]
        spack_path = self.path / "spack"

        # set general build and configuration meta data for the project
        self.configuration_meta = recipe

        # set the environment view meta data
        self.environment_meta = recipe

        # Clone the spack repository if it has not already been checked out
        if not (spack_path / ".git").is_dir():
            self._logger.info(f'spack: clone repository {spack["repo"]}')

            # clone the repository
            capture = subprocess.run(
                ["git", "clone", "--filter=tree:0", spack["repo"], spack_path],
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self._logger.debug(capture.stdout.decode("utf-8"))

            if capture.returncode != 0:
                self._logger.error(f'error cloning the repository {spack["repo"]}')
                capture.check_returncode()

        # Fetch the specific branch
        if spack["commit"]:
            self._logger.info(f'spack: fetch branch/commit {spack["commit"]}')
            capture = subprocess.run(
                ["git", "-C", spack_path, "fetch", "origin", spack["commit"]],
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self._logger.debug(capture.stdout.decode("utf-8"))

            if capture.returncode != 0:
                self._logger.debug(f'unable to change to the fetch {spack["commit"]}')
                capture.check_returncode()

        # Check out a branch or commit if one was specified
        if spack["commit"]:
            self._logger.info(f'spack: checkout branch/commit {spack["commit"]}')
            capture = subprocess.run(
                ["git", "-C", spack_path, "checkout", spack["commit"]],
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self._logger.debug(capture.stdout.decode("utf-8"))

            if capture.returncode != 0:
                self._logger.debug(f'unable to change to the requested commit {spack["commit"]}')
                capture.check_returncode()

        # get the spack commit
        git_commit_result = subprocess.run(
            ["git", "-C", spack_path, "rev-parse", "HEAD"], shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        spack_meta = {
            "ref": spack["commit"],
            "commit": git_commit_result.stdout.strip().decode("utf-8"),
            "url": spack["repo"],
        }

        # load the jinja templating environment
        template_path = self.root / "templates"
        jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(template_path),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # generate top level makefiles
        makefile_template = jinja_env.get_template("Makefile")

        with (self.path / "Makefile").open("w") as f:
            f.write(
                makefile_template.render(
                    cache=recipe.mirror,
                    modules=recipe.config["modules"],
                    post_install_hook=recipe.post_install_hook,
                    pre_install_hook=recipe.pre_install_hook,
                    spack_version=spack_version,
                    spack_meta=spack_meta,
                    exclude_from_cache=["nvhpc", "cuda", "perl"],
                    verbose=False,
                )
            )
            f.write("\n")

        make_user_template = jinja_env.get_template("Make.user")
        with (self.path / "Make.user").open("w") as f:
            f.write(
                make_user_template.render(
                    spack_version=spack_version,
                    build_path=self.path,
                    store=recipe.mount,
                    no_bwrap=recipe.no_bwrap,
                    verbose=False,
                )
            )
            f.write("\n")

        etc_path = self.root / "etc"
        for f_etc in ["Make.inc", "bwrap-mutable-root.sh", "envvars.py"]:
            shutil.copy2(etc_path / f_etc, self.path / f_etc)

        # used to configure both pre and post install hooks, if they are provided.
        hook_env = {
            "mount": recipe.mount,
            "config": recipe.mount / "config",
            "build": self.path,
            "spack": self.path / "spack",
        }

        # copy post install hook file, if provided
        post_hook = recipe.post_install_hook
        if post_hook is not None:
            self._logger.debug("installing post-install-hook script")
            jinja_recipe_env = jinja2.Environment(loader=jinja2.FileSystemLoader(recipe.path))
            post_hook_template = jinja_recipe_env.get_template("post-install")
            post_hook_destination = store_path / "post-install-hook"

            with post_hook_destination.open("w") as f:
                f.write(post_hook_template.render(env=hook_env, verbose=False))
                f.write("\n")

            os.chmod(
                post_hook_destination,
                os.stat(post_hook_destination).st_mode | stat.S_IEXEC,
            )

        # copy pre install hook file, if provided
        pre_hook = recipe.pre_install_hook
        if pre_hook is not None:
            self._logger.debug("installing pre-install-hook script")
            jinja_recipe_env = jinja2.Environment(loader=jinja2.FileSystemLoader(recipe.path))
            pre_hook_template = jinja_recipe_env.get_template("pre-install")
            pre_hook_destination = store_path / "pre-install-hook"

            with pre_hook_destination.open("w") as f:
                f.write(pre_hook_template.render(env=hook_env, verbose=False))
                f.write("\n")

            os.chmod(
                pre_hook_destination,
                os.stat(pre_hook_destination).st_mode | stat.S_IEXEC,
            )

        # Generate the system configuration: the compilers, environments, etc.
        # that are defined for the target cluster.
        config_path = self.path / "config"
        config_path.mkdir(exist_ok=True)
        system_config_path = pathlib.Path(recipe.system_config_path)

        # Copy the yaml files to the spack config path
        for f_config in system_config_path.iterdir():
            # print warning if mirrors.yaml is found
            if f_config.name in ["mirrors.yaml"]:
                self._logger.error(
                    "mirrors.yaml have been removed from cluster configurations,"
                    " use the --cache option on stack-config instead."
                )
                raise RuntimeError("Unsupported mirrors.yaml file in cluster configuration.")

            # construct full file path
            src = system_config_path / f_config.name
            dst = config_path / f_config.name
            # copy only files
            if src.is_file():
                shutil.copy(src, dst)

        # generate a mirrors.yaml file if build caches have been configured
        if recipe.mirror:
            dst = config_path / "mirrors.yaml"
            self._logger.debug(f"generate the build cache mirror: {dst}")
            with dst.open("w") as fid:
                fid.write(cache.generate_mirrors_yaml(recipe.mirror))

        packages_data = {}
        # append network packages to packages.yaml
        network_config_path = system_config_path / "network.yaml"
        if not network_config_path.is_file():
            raise FileNotFoundError(f"The network configuration file '{network_config_path}' does not exist")
        with network_config_path.open() as fid:
            network_config = yaml.load(fid, Loader=yaml.Loader)
            packages_data.update(network_config.get("packages", {}))

        # append recipe packages to packages.yaml
        if recipe.packages:
            system_packages = system_config_path / "packages.yaml"
            if system_packages.is_file():
                # load system yaml
                with system_packages.open() as fid:
                    raw = yaml.load(fid, Loader=yaml.Loader)
                    packages_data.update(raw["packages"])
            packages_data.update(recipe.packages["packages"])
            packages_yaml = yaml.dump({"packages": packages_data})
            packages_path = config_path / "packages.yaml"
            with packages_path.open("w") as fid:
                fid.write(packages_yaml)

        # validate the recipe mpi selection
        for name, config in recipe.environments.items():
            # config[mpi] holds the user specified settings in environment.yaml
            if config["mpi"]:
                mpi = config["mpi"]
                user_mpi_spec = mpi["spec"]
                user_mpi_gpu = mpi["gpu"]
                user_mpi_xspec = mpi["xspec"] if "xspec" in mpi else None
                user_mpi_deps = mpi["depends"] if "depends" in mpi else None
                self._logger.debug(
                    f"User MPI selection: spec={user_mpi_spec}, gpu={user_mpi_gpu}, "
                    f"xspec={user_mpi_xspec}, deps={user_mpi_deps}"
                )

                if user_mpi_spec:
                    try:
                        mpi_impl, mpi_ver = user_mpi_spec.strip().split(sep="@", maxsplit=1)
                    except ValueError:
                        mpi_impl = user_mpi_spec.strip()
                        mpi_ver = None

                    # network_config holds the system specified settings in cluster config / network.yaml
                    if mpi_impl in network_config["mpi_supported"]:
                        default_ver = network_config[mpi_impl]["version"]
                        default_spec = network_config[mpi_impl]["spec"] if "spec" in network_config[mpi_impl] else ""
                        default_deps = (
                            network_config[mpi_impl]["depends"] if "depends" in network_config[mpi_impl] else ""
                        )
                        self._logger.debug(
                            f"System MPI selection: spec={mpi_impl}@{default_ver} {default_spec}, deps={default_deps}"
                        )

                        # select users versions or the default versions if user did not specify
                        version_opt = f"@{mpi_ver or default_ver}"
                        # create full spec based on user provided spec or default spec
                        spec_opt = f"{mpi_impl}{version_opt} {user_mpi_xspec or default_spec}"
                        if user_mpi_gpu and user_mpi_gpu not in spec_opt:
                            spec_opt = f"{spec_opt} +{user_mpi_gpu}"
                        deps_opt = user_mpi_deps or default_deps
                        for dep in deps_opt:
                            spec_opt = f"{spec_opt} ^{dep}"
                        self._logger.debug(f"Final  MPI selection: spec={spec_opt}")

                        recipe.environments[name]["specs"].append(spec_opt)
                    else:
                        # TODO: Create a custom exception type
                        raise Exception(f"Unsupported mpi: {mpi_impl}")

        # Add custom spack package recipes, configured via Spack repos.
        # Step 1: copy Spack repos to store_path where they will be used to
        #         build the stack, and then be part of the upstream provided
        #         to users of the stack.
        #
        # Packages in the recipe are prioritised over cluster specific packages,
        # etc. The order of preference from highest to lowest is:
        #
        # 3. recipe/repo
        # 2. cluster-config/repos.yaml
        #   - if the repos.yaml file exists it will contain a list of relative paths
        #     to search for package
        # 1. spack/var/spack/repos/builtin

        # Build a list of repos with packages to install.
        repos = []

        # check for a repo in the recipe
        if recipe.spack_repo is not None:
            self._logger.debug(f"adding recipe spack package repo: {recipe.spack_repo}")
            repos.append(recipe.spack_repo)

        # look for repos.yaml file in the system configuration
        repo_yaml = system_config_path / "repos.yaml"
        if repo_yaml.exists() and repo_yaml.is_file():
            # open repos.yaml file and reat the list of repos
            with repo_yaml.open() as fid:
                raw = yaml.load(fid, Loader=yaml.Loader)
                P = raw["repos"]

            self._logger.debug(f"the system configuration has a repo file {repo_yaml} refers to {P}")

            # test each path
            for rel_path in P:
                repo_path = (system_config_path / rel_path).resolve()
                if spack_util.is_repo(repo_path):
                    repos.append(repo_path)
                    self._logger.debug(f"adding site spack package repo: {repo_path}")
                else:
                    self._logger.error(f"{repo_path} from {repo_yaml} is not a spack package repository")
                    raise RuntimeError("invalid system-provided package repository")

        self._logger.debug(f"full list of spack package repo: {repos}")

        # Delete the store/repo path, if it already exists.
        # Do this so that incremental builds (though not officially supported) won't break if a repo is updated.
        repo_dst = store_path / "repo"
        self._logger.debug(f"creating the stack spack prepo in {repo_dst}")
        if repo_dst.exists():
            self._logger.debug(f"{repo_dst} exists ... deleting")
            shutil.rmtree(repo_dst)

        # create the repository step 1: create the repo directory
        pkg_dst = repo_dst / "packages"
        pkg_dst.mkdir(mode=0o755, parents=True)
        self._logger.debug(f"created the repo packages path {pkg_dst}")

        # create the repository step 2: create the repo.yaml file that
        # configures the repo.
        with (repo_dst / "repo.yaml").open("w") as f:
            f.write(
                """\
repo:
  namespace: alps
"""
            )

        # create the repository step 2: create the repos.yaml file in build_path/config
        repos_yaml_template = jinja_env.get_template("repos.yaml")
        with (config_path / "repos.yaml").open("w") as f:
            repo_path = recipe.mount / "repo"
            f.write(repos_yaml_template.render(repo_path=repo_path.as_posix(), verbose=False))
            f.write("\n")

        # Iterate over the source repositories copying their contents to the consolidated repo in the uenv.
        # Do overwrite packages that have been copied from an earlier source repo, enforcing a descending
        # order of precidence.
        if len(repos) > 0:
            for repo_src in repos:
                self._logger.debug(f"installing repo {repo_src}")
                packages_path = repo_src / "packages"
                for pkg_path in packages_path.iterdir():
                    dst = pkg_dst / pkg_path.name
                    if pkg_path.is_dir() and not dst.exists():
                        self._logger.debug(f"  installing package {pkg_path} to {pkg_dst}")
                        install(pkg_path, dst)
                    elif dst.exists():
                        self._logger.debug(f"  NOT installing package {pkg_path}")

        # Generate the makefile and spack.yaml files that describe the compilers
        compiler_files = recipe.compiler_files
        compiler_path = self.path / "compilers"
        compiler_path.mkdir(exist_ok=True)
        with (compiler_path / "Makefile").open(mode="w") as f:
            f.write(compiler_files["makefile"])

        for name, yml in compiler_files["config"].items():
            compiler_config_path = compiler_path / name
            compiler_config_path.mkdir(exist_ok=True)
            with (compiler_config_path / "spack.yaml").open(mode="w") as f:
                f.write(yml)

        # generate the makefile and spack.yaml files that describe the environments
        environment_files = recipe.environment_files
        environments_path = self.path / "environments"
        os.makedirs(environments_path, exist_ok=True)
        with (environments_path / "Makefile").open(mode="w") as f:
            f.write(environment_files["makefile"])

        for name, yml in environment_files["config"].items():
            env_config_path = environments_path / name
            env_config_path.mkdir(exist_ok=True)
            with (env_config_path / "spack.yaml").open(mode="w") as f:
                f.write(yml)

        # generate the makefile that generates the configuration for the spack
        # installation in the generate-config sub-directory of the build path.
        make_config_template = jinja_env.get_template("Makefile.generate-config")
        generate_config_path = self.path / "generate-config"
        generate_config_path.mkdir(exist_ok=True)

        # write generate-config/Makefile
        all_compilers = [x for x in recipe.compilers.keys()]
        release_compilers = [x for x in all_compilers if x != "bootstrap"]
        with (generate_config_path / "Makefile").open("w") as f:
            f.write(
                make_config_template.render(
                    build_path=self.path.as_posix(),
                    all_compilers=all_compilers,
                    release_compilers=release_compilers,
                    verbose=False,
                )
            )

        # write modules/modules.yaml
        modules_yaml = recipe.modules_yaml
        generate_modules_path = self.path / "modules"
        generate_modules_path.mkdir(exist_ok=True)
        with (generate_modules_path / "modules.yaml").open("w") as f:
            f.write(modules_yaml)

        # write the meta data
        meta_path = store_path / "meta"
        meta_path.mkdir(exist_ok=True)
        # write a json file with basic meta data
        with (meta_path / "configure.json").open("w") as f:
            # default serialisation is str to serialise the pathlib.PosixPath
            f.write(json.dumps(self.configuration_meta, sort_keys=True, indent=2, default=str))
            f.write("\n")

        # write a json file with the environment view meta data
        with (meta_path / "env.json.in").open("w") as f:
            # default serialisation is str to serialise the pathlib.PosixPath
            f.write(json.dumps(self.environment_meta, sort_keys=True, indent=2, default=str))
            f.write("\n")

        # copy the recipe to a recipe subdirectory of the meta path
        meta_recipe_path = meta_path / "recipe"
        meta_recipe_path.mkdir(exist_ok=True)
        if meta_recipe_path.exists():
            shutil.rmtree(meta_recipe_path)
        install(recipe.path, meta_recipe_path, ignore=shutil.ignore_patterns(".git"))

        # create the meta/extra path and copy recipe meta data if it exists
        meta_extra_path = meta_path / "extra"
        meta_extra_path.mkdir(exist_ok=True)
        if meta_extra_path.exists():
            shutil.rmtree(meta_extra_path)
        if recipe.user_extra is not None:
            self._logger.debug(f"copying extra recipe meta data to {meta_extra_path}")
            install(recipe.user_extra, meta_extra_path)

        # create debug helper script
        debug_script_path = self.path / "stack-debug.sh"
        debug_script_template = jinja_env.get_template("stack-debug.sh")
        with debug_script_path.open("w") as f:
            f.write(
                debug_script_template.render(
                    mount_path=recipe.mount,
                    build_path=str(self.path),
                    use_bwrap=not recipe.no_bwrap,
                    spack_version=spack_version,
                    verbose=False,
                )
            )
            f.write("\n")
