import logging
import os
import pathlib
import shutil
import subprocess

import jinja2

from . import root_logger


class Builder:
    def __init__(self, args):
        self._logger = root_logger 
        path = pathlib.Path(args.build)
        if not path.is_absolute():
            path = pathlib.Path.cwd() / path

        if path.exists():
            if not path.is_dir():
                raise IOError('build path is not a directory')

        self.path = path
        self.root = prefix = pathlib.Path(__file__).parent.parent.resolve()

    def generate(self, recipe):
        # make the paths
        store_path = self.path / 'store'
        tmp_path = self.path / 'tmp'

        self.path.mkdir(exist_ok=True, parents=True)
        store_path.mkdir(exist_ok=True)
        tmp_path.mkdir(exist_ok=True)

        # check out the version of spack
        spack = recipe.config['spack']
        spack_path = self.path / 'spack'

        # Clone the spack repository if it has not already been checked out
        if not (spack_path / '.git').is_dir():
            self._logger.info(f'spack: clone repository {spack["repo"]}')

            # clone the repository
            capture = subprocess.run(
                ["git", "clone", spack['repo'], spack_path],
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)
            self._logger.debug(capture.stdout.decode('utf-8'))

            if capture.returncode != 0:
                self._logger.debug(f'error cloning the repository {spack["repo"]}')
                capture.check_returncode()

        # Check out a branch or commit if one was specified
        if spack['commit']:
            self._logger.info(f'spack: checkout branch/commit {spack["commit"]}')
            capture = subprocess.run(
                ["git", "-C", spack_path, "checkout", spack['commit']],
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)
            self._logger.debug(capture.stdout.decode('utf-8'))

            if capture.returncode != 0:
                self._logger.debug(
                    f'unable to change to the requested commit {spack["commit"]}')
                capture.check_returncode()

        # load the jinja templating environment
        template_path = self.root / 'templates'
        env = jinja2.Environment(
            loader = jinja2.FileSystemLoader(template_path),
            trim_blocks=True, lstrip_blocks=True)

        # generate top level makefiles
        makefile_template = env.get_template('Makefile')
        with (self.path / 'Makefile').open('w') as f:
            cache = {'key': recipe.mirror.key, 'enabled': recipe.mirror.source}
            f.write(makefile_template.render(
                cache=cache, modules=recipe.config['modules'], verbose=False))
            f.write('\n')

        make_user_template = env.get_template('Make.user')
        with (self.path / 'Make.user').open('w') as f:
            f.write(make_user_template.render(
                build_path=self.path, store=recipe.config['store'],
                verbose=False))
            f.write('\n')

        etc_path = self.root / 'etc'
        for f in ['Make.inc', 'bwrap-mutable-root.sh']:
            shutil.copy2(etc_path / f, self.path / f)

        # Generate the system configuration: the compilers, environments,
        # mirrors etc. that are defined for the target cluster.
        config_path = self.path / 'config'
        config_path.mkdir(exist_ok=True)
        system_configs_path = pathlib.Path(recipe.configs_path)

        # Copy the yaml files to the spack config path
        for f in system_configs_path.iterdir():
            # skip copying mirrors.yaml - this is done in the next step only if
            # mirrors have been enabled and the recipe did not provide a mirror
            # configuration
            if f.name in ['mirrors.yaml']:
                continue

            # construct full file path
            src = system_configs_path / f.name
            dst = config_path / f.name
            # copy only files
            if src.is_file():
                shutil.copy(src, dst)

        # copy the optional mirrors.yaml file
        if recipe.mirror.source:
            dst = config_path / 'mirrors.yaml'
            shutil.copy(recipe.mirror.source, dst)

        # append recipe packages to packages.yaml
        if recipe.packages:
            system_packages = system_configs_path / 'packages.yaml'
            packages_data = {}
            if system_packages.is_file():
                # load system yaml
                with system_packages.open() as fid:
                    raw = yaml.load(fid, Loader=yaml.Loader)
                    packages_data = raw['packages']
            packages_data.update(recipe.packages['packages'])
            packages_yaml = yaml.dump({'packages': packages_data})
            packages_path = config_path / 'packages.yaml'
            with packages_path.open("w") as fid:
                fid.write(packages_yaml)

        # Configure the CSCS custom spack environments.
        # Step 1: copy the CSCS repo to store_path where, it will be used to
        #         build the stack, and then be part of the upstream provided
        #         to users of the stack.
        repo_src = self.root / 'repo'
        repo_dst = store_path / 'repo'
        if repo_dst.exists():
            shutil.rmtree(repo_dst)

        shutil.copytree(repo_src, repo_dst)

        # Step 2: Create a repos.yaml file in build_path/config
        repos_yaml_template = env.get_template('repos.yaml')
        with (config_path / 'repos.yaml').open('w') as f:
            repo_path = pathlib.Path(recipe.config['store']) / 'repo'
            f.write(repos_yaml_template.render(
                repo_path=repo_path.as_posix(),verbose=False))
            f.write('\n')

        # Generate the makefile and spack.yaml files that describe the compilers
        compilers = recipe.generate_compilers()
        compiler_path = self.path / 'compilers'
        compiler_path.mkdir(exist_ok=True)
        with (compiler_path / 'Makefile').open(mode='w') as f:
            f.write(compilers['makefile'])

        for name, yml in compilers['config'].items():
            compiler_config_path = compiler_path / name
            compiler_config_path.mkdir(exist_ok=True)
            with (compiler_config_path / 'spack.yaml').open(mode='w') as f:
                f.write(yml)

        # generate the makefile and spack.yaml files that describe the environments
        environments = recipe.generate_environments()
        environments_path = self.path / 'environments'
        os.makedirs(environments_path, exist_ok=True)
        with (environments_path / 'Makefile').open(mode='w') as f:
            f.write(environments['makefile'])

        for name, yml in environments['config'].items():
            env_config_path = environments_path / name
            env_config_path.mkdir(exist_ok=True)
            with (env_config_path / 'spack.yaml').open(mode='w') as f:
                f.write(yml)

        # generate the makefile that generates the configuration for the spack
        # installation
        make_config_template = env.get_template('Makefile.generate-config')
        generate_config_path = self.path / 'generate-config'
        generate_config_path.mkdir(exist_ok=True)

        # write the Makefile
        all_compilers=[x for x in recipe.compilers.keys()]
        release_compilers=[x for x in all_compilers if x != "bootstrap"]
        with (generate_config_path / 'Makefile').open('w') as f:
            f.write(make_config_template.render(
                build_path=self.path.as_posix(),
                all_compilers=all_compilers,
                release_compilers=release_compilers,
                verbose=False))

        # write the modules.yaml file
        modules_yaml=recipe.generate_modules()
        generate_modules_path = self.path / 'modules'
        generate_modules_path.mkdir(exist_ok=True)
        with (generate_modules_path / 'modules.yaml').open('w') as f:
            f.write(modules_yaml)
