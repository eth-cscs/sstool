import copy
import pathlib

import jinja2
import yaml

from . import cache, root_logger, schema, spack_util


class Recipe:
    valid_mpi_specs = {
        "cray-mpich": (None, None),
        "mpich": ("4.1", "device=ch4 netmod=ofi +slurm"),
        "mvapich2": (
            "3.0a",
            "+xpmem fabrics=ch4ofi ch4_max_vcis=4 process_managers=slurm",
        ),
        "openmpi": ("5", "+internal-pmix +legacylaunchers +orterunprefix fabrics=cma,ofi,xpmem schedulers=slurm"),
    }

    @property
    def path(self):
        """the path of the recipe"""
        return self._path

    @path.setter
    def path(self, recipe_path):
        path = pathlib.Path(recipe_path)
        if not path.is_absolute():
            path = pathlib.Path.cwd() / path

        if not path.is_dir():
            raise FileNotFoundError(f"The recipe path '{path}' does not exist")

        self._path = path

    def __init__(self, args):
        self._logger = root_logger
        self._logger.debug("Generating recipe")

        self.no_bwrap = args.no_bwrap

        # set the system configuration path
        self.system_config_path = args.system

        # set the recipe path
        self.path = args.recipe

        self.template_path = pathlib.Path(__file__).parent.resolve() / "templates"

        # required config.yaml file
        self.config = self.path / "config.yaml"

        # set the recipe-defined mount point
        if args.mount:
            self.config["store"] = args.mount

        # ensure that the requested mount point exists
        if not self.mount.is_dir():
            raise FileNotFoundError(f"the mount point '{self.mount}' must exist")

        # required compilers.yaml file
        compiler_path = self.path / "compilers.yaml"
        self._logger.debug(f"opening {compiler_path}")
        if not compiler_path.is_file():
            raise FileNotFoundError(f"The recipe path '{compiler_path}' does not contain compilers.yaml")

        with compiler_path.open() as fid:
            raw = yaml.load(fid, Loader=yaml.Loader)
            schema.compilers_validator.validate(raw)
            self.generate_compiler_specs(raw)

        # required environments.yaml file
        environments_path = self.path / "environments.yaml"
        self._logger.debug(f"opening {environments_path}")
        if not environments_path.is_file():
            raise FileNotFoundError(f"The recipe path '{environments_path}' does not contain environments.yaml")

        with environments_path.open() as fid:
            raw = yaml.load(fid, Loader=yaml.Loader)
            schema.environments_validator.validate(raw)
            self.generate_environment_specs(raw)

        # optional modules.yaml file
        modules_path = self.path / "modules.yaml"
        self._logger.debug(f"opening {modules_path}")
        if not modules_path.is_file():
            modules_path = pathlib.Path(args.build) / "spack/etc/spack/defaults/modules.yaml"
            self._logger.debug(f"no modules.yaml provided - using the {modules_path}")

        self.modules = modules_path

        # optional packages.yaml file
        packages_path = self.path / "packages.yaml"
        self._logger.debug(f"opening {packages_path}")
        self.packages = None
        if packages_path.is_file():
            with packages_path.open() as fid:
                self.packages = yaml.load(fid, Loader=yaml.Loader)

        # optional mirror configurtion
        mirrors_path = self.path / "mirrors.yaml"
        if mirrors_path.is_file():
            self._logger.warning(
                "mirrors.yaml have been removed from recipes, use the --cache option on stack-config instead."
            )
            raise RuntimeError("Unsupported mirrors.yaml file in recipe.")

        self.mirror = (args.cache, self.mount)

        # optional post install hook
        if self.post_install_hook is not None:
            self._logger.debug(f"post install hook {self.post_install_hook}")
        else:
            self._logger.debug("no post install hook provided")

        # optional pre install hook
        if self.pre_install_hook is not None:
            self._logger.debug(f"pre install hook {self.pre_install_hook}")
        else:
            self._logger.debug("no pre install hook provided")

        # determine the version of spack being used:
        # --develop flag implies the next release of spack
        # --spack-version option explicitly sets the version
        # otherwise the name of the commit provided in the config.yaml file is inspected
        self.spack_version = self.find_spack_version(args.develop, args.spack_version)

    # Returns:
    #   Path: if the recipe contains a spack package repository
    #   None: if there is the recipe contains no repo
    @property
    def spack_repo(self):
        repo_path = self.path / "repo"
        if spack_util.is_repo(repo_path):
            return repo_path
        return None

    # Returns:
    #   Path: of the recipe extra path if it exists
    #   None: if there is no user-provided extra path in the recipe
    @property
    def user_extra(self):
        extra_path = self.path / "extra"
        if extra_path.exists() and extra_path.is_dir():
            return extra_path
        return None

    # Returns:
    #   Path: of the recipe post install script if it was provided
    #   None: if there is no user-provided post install script
    @property
    def post_install_hook(self):
        hook_path = self.path / "post-install"
        if hook_path.exists() and hook_path.is_file():
            return hook_path
        return None

    # Returns:
    #   Path: of the recipe pre install script if it was provided
    #   None: if there is no user-provided pre install script
    @property
    def pre_install_hook(self):
        hook_path = self.path / "pre-install"
        if hook_path.exists() and hook_path.is_file():
            return hook_path
        return None

    # Returns a dictionary with the following fields
    #
    # root: /path/to/cache
    # path: /path/to/cache/user-environment
    # key: /path/to/private-pgp-key
    @property
    def mirror(self):
        return self._mirror

    # configuration is a tuple with two fields:
    # - a Path of the yaml file containing the cache configuration
    # - the mount point of the image
    @mirror.setter
    def mirror(self, configuration):
        self._logger.debug(f"configuring build cache mirror with {configuration}")
        self._mirror = None

        file, mount = configuration

        if file is not None:
            mirror_config_path = pathlib.Path(file)
            if not mirror_config_path.is_file():
                raise FileNotFoundError(f"The cache configuration '{file}' is not a file")

            self._mirror = cache.configuration_from_file(mirror_config_path, pathlib.Path(mount))

    @property
    def config(self):
        return self._config

    @config.setter
    def config(self, config_path):
        self._logger.debug(f"opening {config_path}")
        if not config_path.is_file():
            raise FileNotFoundError(f"The recipe path '{config_path}' does not contain config.yaml")

        with config_path.open() as fid:
            raw = yaml.load(fid, Loader=yaml.Loader)
            schema.config_validator.validate(raw)
            self._config = raw

    def find_spack_version(self, develop, spack_version):
        # determine the "major" version, if it can be inferred.
        # one of "0.21", "0.22", "0.23", "0.24" or "unknown".
        # "0.24" implies the latest features in develop that will
        # are being developed for the next version of spack

        # the user has explicitly requested develop:
        if develop:
            return "0.24"

        if spack_version is not None:
            return spack_version

        # infer from the branch name
        # Note: this could be improved by first downloading
        # the requested spack version/tag/commit, then checking
        # the version returned by `spack --version`
        #
        # this would require defering this decision until after
        # the repo is cloned in build.py... a lot of work.
        commit = self.config["spack"]["commit"]
        if commit is None or commit == "develop":
            return "0.24"
        # currently supported
        if commit.find("0.24") >= 0:
            return "0.24"
        # currently supported
        if commit.find("0.23") >= 0:
            return "0.23"
        # currently supported
        if commit.find("0.22") >= 0:
            return "0.22"
        # currently supported
        if commit.find("0.21") >= 0:
            return "0.21"
        # currently supported
        if commit.find("0.20") >= 0:
            raise ValueError(f"spack minimum version is v0.21 - recipe uses {commit}")

        return "unknown"

    @property
    def environment_view_meta(self):
        # generate the view meta data that is presented in the squashfs image meta data
        view_meta = {}
        for _, env in self.environments.items():
            view = env["view"]
            if view is not None:
                view_meta[view["name"]] = {
                    "root": view["config"]["root"],
                    "activate": view["config"]["root"] + "/activate.sh",
                    "description": "",  # leave the description empty for now
                }

        return view_meta

    @property
    def modules_yaml(self):
        with self.modules.open() as fid:
            raw = yaml.load(fid, Loader=yaml.Loader)
            raw["modules"]["default"]["roots"]["tcl"] = (pathlib.Path(self.mount) / "modules").as_posix()
            return yaml.dump(raw)

    # creates the self.environments field that describes the full specifications
    # for all of the environments sets, grouped in environments, from the raw
    # environments.yaml input.
    def generate_environment_specs(self, raw):
        environments = raw

        # enumerate large binary packages that should not be pushed to binary caches
        for _, config in environments.items():
            config["exclude_from_cache"] = ["cuda", "nvhpc", "perl"]

        # check the environment descriptions and ammend where features are missing
        for name, config in environments.items():
            if ("specs" not in config) or (config["specs"] is None):
                environments[name]["specs"] = []

            if "mpi" not in config:
                environments[name]["mpi"] = {"spec": None, "gpu": None}

        # complete configuration of MPI in each environment
        for name, config in environments.items():
            if config["mpi"]:
                mpi = config["mpi"]
                mpi_spec = mpi["spec"]
                mpi_gpu = mpi["gpu"]
                if mpi_spec:
                    try:
                        mpi_impl, mpi_ver = mpi_spec.strip().split(sep="@", maxsplit=1)
                    except ValueError:
                        mpi_impl = mpi_spec.strip()
                        mpi_ver = None

                    if mpi_impl in Recipe.valid_mpi_specs:
                        default_ver, options = Recipe.valid_mpi_specs[mpi_impl]
                        if mpi_ver:
                            version_opt = f"@{mpi_ver}"
                        else:
                            version_opt = f"@{default_ver}" if default_ver else ""

                        spec = f"{mpi_impl}{version_opt} {options or ''}".strip()

                        if mpi_gpu:
                            spec = f"{spec} +{mpi_gpu}"

                        environments[name]["specs"].append(spec)
                    else:
                        # TODO: Create a custom exception type
                        raise Exception(f"Unsupported mpi: {mpi_impl}")

        # set constraints that ensure the the main compiler is always used to build packages
        # that do not explicitly request a compiler.
        for name, config in environments.items():
            compilers = config["compiler"]
            if len(compilers) == 1:
                config["toolchain_constraints"] = []
                continue
            requires = [f"%{compilers[0]['spec']}"]
            for spec in config["specs"]:
                if "%" in spec:
                    requires.append(spec)

            config["toolchain_constraints"] = requires

        # An awkward hack to work around spack not supporting creating activation
        # scripts for each file system view in an environment: it only generates them
        # for the "default" view.
        # The workaround is to create multiple versions of the same environment, one
        # for each view.
        # TODO: remove when the minimum supported version of spack is v0.21, in which
        # this issue was fixed, see https://github.com/spack/spack/pull/40549
        # we have a `--develop` workaround that uses the current approach of generating
        # a separate environment for each view, with a view named "default", and uses
        # the name default to generated the activation script.
        env_names = set()
        env_name_map = {}
        for name, config in environments.items():
            env_name_map[name] = []
            for view, vc in config["views"].items():
                if view in env_names:
                    raise Exception(f"An environment view with the name '{view}' already exists.")
                # set some default values:
                # vc["link"] = "roots"
                # vc["uenv"]["add_compilers"] = True
                # vc["uenv"]["prefix_paths"] = {}
                if vc is None:
                    vc = {}
                vc.setdefault("link", "roots")
                vc.setdefault("uenv", {})
                vc["uenv"].setdefault("add_compilers", True)
                vc["uenv"].setdefault("prefix_paths", {})
                prefix_string = ",".join(
                    [f"{name}={':'.join(paths)}" for name, paths in vc["uenv"]["prefix_paths"].items()]
                )
                vc["uenv"]["prefix_string"] = prefix_string
                # save a copy of the view configuration
                env_name_map[name].append((view, vc))

        # Iterate over each environment:
        # - creating copies of the env so that there is one copy per view.
        # - configure each view
        for name, views in env_name_map.items():
            numviews = len(env_name_map[name])

            # The configuration of the environment without views
            base = copy.deepcopy(environments[name])

            environments[name]["view"] = None
            for i in range(numviews):
                # pick a name for the environment
                cname = name if i == 0 else name + f"-{i + 1}__"
                if i > 0:
                    environments[cname] = copy.deepcopy(base)

                view_name, view_config = views[i]
                # note: the root path is stored as a string, not as a pathlib.PosixPath
                # to avoid serialisation issues when generating the spack.yaml file for
                # each environment.
                if view_config is None:
                    view_config = {"root": str(self.mount / "env" / view_name)}
                else:
                    view_config["root"] = str(self.mount / "env" / view_name)

                # The "uenv" field is not spack configuration, it is additional information
                # used by stackinator additionally set compiler paths and LD_LIBRARY_PATH
                # Remove it from the view_config that will be passed directly to spack, and pass
                # it separately for configuring the envvars.py helper during the uenv build.
                extra = view_config.pop("uenv")

                environments[cname]["view"] = {"name": view_name, "config": view_config, "extra": extra}

        self.environments = environments

    # creates the self.compilers field that describes the full specifications
    # for all of teh compilers from the raw compilers.yaml input
    def generate_compiler_specs(self, raw):
        compilers = {}

        bootstrap = {}
        bootstrap["packages"] = {
            "external": [
                "perl",
                "m4",
                "autoconf",
                "automake",
                "libtool",
                "gawk",
                "python",
                "texinfo",
                "gawk",
            ],
        }
        bootstrap_spec = raw["bootstrap"]["spec"]
        bootstrap["specs"] = [
            f"{bootstrap_spec} languages=c,c++",
            "squashfs default_compression=zstd",
        ]
        bootstrap["exclude_from_cache"] = ["cuda", "nvhpc", "perl"]
        compilers["bootstrap"] = bootstrap

        gcc = {}
        gcc["packages"] = {
            "external": [
                "perl",
                "m4",
                "autoconf",
                "automake",
                "libtool",
                "gawk",
                "python",
                "texinfo",
                "gawk",
            ],
        }
        gcc["specs"] = raw["gcc"]["specs"]
        gcc["requires"] = bootstrap_spec
        gcc["exclude_from_cache"] = ["cuda", "nvhpc", "perl"]
        compilers["gcc"] = gcc
        if raw["llvm"] is not None:
            llvm = {}
            llvm["packages"] = False
            llvm["specs"] = []
            for spec in raw["llvm"]["specs"]:
                if spec.startswith("nvhpc"):
                    llvm["specs"].append(f"{spec}~mpi~blas~lapack")

                if spec.startswith("llvm"):
                    llvm["specs"].append(f"{spec} +clang targets=x86 ~gold ^ninja@kitware")

            llvm["requires"] = raw["llvm"]["requires"]
            llvm["exclude_from_cache"] = ["cuda", "nvhpc", "perl"]
            compilers["llvm"] = llvm

        self.compilers = compilers

    # The path of the default configuration for the target system/cluster
    @property
    def system_config_path(self):
        return self._system_path

    @system_config_path.setter
    def system_config_path(self, path):
        system_path = pathlib.Path(path)
        if not system_path.is_absolute():
            system_path = pathlib.Path.cwd() / system_path

        if not system_path.is_dir():
            raise FileNotFoundError(f"The system configuration path '{system_path}' does not exist")

        self._system_path = system_path

    @property
    def mount(self):
        return pathlib.Path(self.config["store"])

    @property
    def compiler_files(self):
        files = {}

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(self.template_path),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        makefile_template = env.get_template("Makefile.compilers")
        push_to_cache = self.mirror is not None
        files["makefile"] = makefile_template.render(
            compilers=self.compilers,
            push_to_cache=push_to_cache,
            spack_version=self.spack_version,
        )

        # generate compilers/<compiler>/spack.yaml
        files["config"] = {}
        for compiler, config in self.compilers.items():
            spack_yaml_template = env.get_template(f"compilers.{compiler}.spack.yaml")
            files["config"][compiler] = spack_yaml_template.render(config=config)

        return files

    @property
    def environment_files(self):
        files = {}

        jenv = jinja2.Environment(
            loader=jinja2.FileSystemLoader(self.template_path),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        jenv.filters["py2yaml"] = schema.py2yaml

        makefile_template = jenv.get_template("Makefile.environments")
        push_to_cache = self.mirror is not None
        files["makefile"] = makefile_template.render(
            environments=self.environments,
            push_to_cache=push_to_cache,
            spack_version=self.spack_version,
        )

        files["config"] = {}
        for env, config in self.environments.items():
            spack_yaml_template = jenv.get_template("environments.spack.yaml")
            files["config"][env] = spack_yaml_template.render(config=config, name=env, store=self.mount)

        return files
