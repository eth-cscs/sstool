# Recipes

A recipe is a description of all of the compilers and software packages to be installed, along with configuration of modules and environment scripts the stack will provide to users.
A recipe is comprised of the following yaml files in a directory:

* `config.yaml`: common configuration for the stack.
* `compilers.yaml`: the compilers provided by the stack.
* `environments.yaml`: environments that contain all the software packages.
* `modules.yaml`: _optional_ module generation rules
    * follows the spec for [spack module configuration](https://spack.readthedocs.io/en/latest/module_file_support.html)
* `packages.yaml`: _optional_ define external packages
    * follows the spec for [spack package configuration](https://spack.readthedocs.io/en/latest/build_settings.html)
* `repo`: _optional_ custom spack package definitions.
* `extra`: _optional_ additional meta data to copy to the meta data of the stack.
* `post-install`: _optional_ a script to run after Spack has been executed to build the stack.
* `pre-install`: _optional_ a script to run before any packages have been built.

## Configuration

```yaml title="config.yaml"
name: prgenv-gnu
store: /user-environment
spack:
    repo: https://github.com/spack/spack.git
    commit: releases/v0.20
modules: true
description: "HPC development tools for building MPI applications with the GNU compiler toolchain"
```

* `name`: a plain text name for the environment
* `store`: the location where the environment will be mounted.
* `spack`: which spack repository to use for installation.
* `modules`: _optional_ enable/diasble module file generation (default `true`).
* `description`: _optional_ a string that describes the environment (default empty).

## Compilers

Take an example configuration:
```yaml title="compilers.yaml"
bootstrap:
  spec: gcc@11
gcc:
  specs:
  - gcc@11
llvm:
  requires: gcc@11
  specs:
  - nvhpc@21.7
  - llvm@14
```

The compilers are built in multiple stages:

1. *bootstrap*: A bootstrap gcc compiler is built using the system compiler (currently gcc 4.7.5).
    * `gcc:specs`: single spec of the form `gcc@version`.
    * The selected version should have full support for the target architecture in order to build optimised gcc toolchains in step 2.
2. *gcc*: The bootstrap compiler is then used to build the gcc version(s) provided by the stack.
    * `gcc:specs`: A list of _at least one_ of the specs of the form `gcc@version`.
3. *llvm*: (optional) The nvhpc and/or llvm toolchains are built using one of the gcc toolchains installed in step 2.
    * `llvm:specs`: a list of specs of the form `nvhpc@version` or `llvm@version`.
    * `llvm:requires`: the version of gcc from step 2 that is used to build the llvm compilers.

The first two steps are required, so that the simplest stack will provide at least one version of gcc compiled for the target architecture.

!!! note
    Don't provide full specs, because the tool will insert "opinionated" specs for the target node type, for example:

    * `nvhpc@21.7` generates `nvhpc@21.7 ~mpi~blas~lapack`
    * `llvm@14` generates `llvm@14 +clang targets=x86 ~gold ^ninja@kitware`
    * `gcc@11` generates `gcc@11 build_type=Release +profiled +strip`

## Environments

The software packages to install using the compiler toolchains are configured as disjoint environments, each built with the same compiler, and configured with an optional implementation of MPI.
These are specified in the `environments.yaml` file.

For example, consider a workflow that has to build more multiple applications - some of which require Fortran+OpenACC and others that are CPU only C code that can be built with GCC.
To provide a single Spack stack that meets the workflow's needs, we would create two environments, one for each of the `nvhpc` and `gcc` compiler toolchains:

```yaml title="environments.yaml high level overview"
# A GCC-based programming environment
prgenv-gnu:
  compiler:   # ... compiler toolchain
  mpi:        # ... mpi configuration
  deprecated: # ... whether to allow usage of deprecated packages or not
  unify:      # ... configure Spack concretizer
  specs:      # ... list of packages to install
  variants:   # ... variants to apply to packages (e.g. +mpi)
  packages:   # ... list of external packages to use
  views:      # ... environment views to provide to users
# An NVIDIA programming environment
prgenv-nvgpu:
  # ... same structure as prgenv-gnu
```

In the following sections, we will explore each of the environment configuration fields in detail.

### Compilers

The `compiler` field describes a list compilers to use to build the software stack.
Each compiler toolchain is specified using toolchain and spec

```yaml title="compile all packages with gcc@11.3"
  compiler:
  - toolchain: gcc
    spec: gcc@11.3
```

Sometimes two compiler toolchains are required, for example when using the `nvhpc` compilers, there are often dependencies that can't be built using the NVIDIA, or are better being built with GCC (for example `cmake`, `perl` and `netcdf-c`).
The example below uses the `nvhpc` compilers with gcc@11.3.

```yaml title="compile all packages with gcc@11.3"
  compiler:
  - toolchain: gcc
    spec: gcc@11.3
  - toolchain: llvm
    spec: nvhpc@22.7
```

!!! note
    If more than one version of gcc has been installed, use the same version that was used to install `nvhpc`.

!!! warning
    Stackinator does not test or support using two versions of gcc in the same toolchain.

The order of the compilers is significant. The first compiler is the default, and the other compilers will only be used to build packages when explicitly added to a spec.
For example, in the recipe below, only `netcdf-fortran` will be built with the `nvhpc` toolchain, while the root specs `cmake` and `netcdf-c` and all dependencies will be built using the `gcc` toolchain.


```yaml title="compile all packages with gcc@11.3"
  compiler:
  - toolchain: gcc
    spec: gcc
  - toolchain: llvm
    spec: nvhpc
  specs
  - cmake
  - netcdf-c
  - netcdf-fortran%nvhpc
```

!!! note
    This approach is typically used to build Fortran applications and packages with one toolchain (e.g. `nvhpc`), and all of the C/C++ dependencies with a different toolchain (e.g. `gcc`).

### MPI

Stackinator can configure cray-mpich (CUDA, ROCM, or non-GPU aware) or OpenMPI (with or without CUDA) (on a per-environment basis, by setting the `mpi` field in an environment.

!!! note
    Future versions of Stackinator will fully support OpenMPI, MPICH and MVAPICH when (and if) they develop robust support for HPE SlingShot 11 interconnect.

    Current OpenMPI support has been tested lightly and is not guaranteed to be production ready - only OpenMPI@5.x.x is supported (default is @5.0.6 at the time of writing) - CUDA is supported, ROCM has not yet been tested.

If the `mpi` field is not set, or is set to `null`, MPI will not be configured in an environment:
```yaml title="environments.yaml: no MPI"
serial-env:
  mpi: null
  # ...
```

To configure MPI without GPU support, set the `spec` field with an optional version:
```yaml title="environments.yaml: Cray-mpich without GPU support"
host-env:
  mpi:
    spec: cray-mpich@8.1.23
  # ...
```
```yaml title="environments.yaml: OpenMPI without GPU support"
host-env:
  mpi:
    spec: openmpi
  # ...
```

GPU-aware MPI can be configured by setting the optional `gpu` field to specify whether to support `cuda` or `rocm` GPUs:
```yaml title="environments.yaml: GPU aware MPI"
cuda-env:
  mpi:
    spec: cray-mpich
    gpu: cuda
  # ...
rocm-env:
  mpi:
    spec: cray-mpich
    gpu: rocm
  # ...
ompi-cuda-env:
  mpi:
    spec: openmpi
    gpu: cuda
  # ...
```
#### Experimental libfabric 2.x support with cray-mpich
HPE recently open-sourced the libfabric/cxi provider (and related drivers) and this can be built into cray-mpich by adding the `+cxi` variant to the spec
```yaml title="environments.yaml: MPI using new libfabric/cxi stack"
mpich-cxi-env:
  mpi:
    spec: cray-mpich +cxi
    gpu: cuda
  # ...
```
OpenMPI does not provide a `cxi` option since it is mandatory to use it for builds on the alps cluster. Currently the performance of OpenMPI on Alps clusters might not be optimal and work is ongoing to fine tune it especially for intra-node performance.

!!! alps

    As new versions of cray-mpich are released with CPE, they are provided on Alps vClusters, via the Spack package repo in the [CSCS cluster configuration repo](https://github.com/eth-cscs/alps-cluster-config/tree/master/site/repo).
    The following versions of cray-mpich are currently provided:

    |   cray-mpich  |   CPE     |   notes                  |
    | :------------ | :-------- | :----------------------- |
    |  8.1.29       | 24.03     | pre-release  |
    |  8.1.28       | 23.12     | released 2023-12 **default** |
    |  8.1.27       | 23.09     | released 2023-09     |
    |  8.1.26       | 23.06     | released 2023-06     |
    |  8.1.25       | 23.03     | released 2023-02-26  |
    |  8.1.24       | 23.02     | released 2023-01-19  |
    |  8.1.23       | 22.12     | released 2022-11-29  |
    |  8.1.21.1     | 22.11     | released 2022-10-25  |
    |  8.1.18.4     | 22.08     | released 2022-07-21  |

    All versions of cray-mpich in the table have been validated on Alps vClusters with Slingshot 11 and libfabric 1.15.2.

!!! note
    The `cray-mpich` spec is added to the list of package specs automatically, and all packages that use the virtual dependency `+mpi` will use this `cray-mpich`.

### Specs

The list of software packages to install is configured in the `spec:` field of an environment. The specs follow the [standard Spack practice](https://spack.readthedocs.io/en/latest/environments.html#spec-concretization).

The `deprecated: ` field controls if Spack should consider versions marked as deprecated, and can be set to `true` or `false` (for considering or not considering deprecated versions, respectively).

The `unify:` field controls the Spack concretiser, and can be set to three values `true`, `false` or `when_possible`.
The

```yaml
cuda-env:
  specs:
  - cmake
  - hdf5
  - python@3.10
  unify: true
```

To install more than one version of the same package, or to concretise some more challenging combinations of packages, you might have to relax the concretiser to `when_possible` or `false`.
For example, this environment provides `hdf5` both with and without MPI support:

```yaml
cuda-env:
  specs:
  - cmake
  - hdf5~mpi
  - hdf5+mpi
  - python@3.10
  unify: when_possible
```

!!! note
    Use `unify:true` when possible, then `unify:when_possible`, and finally `unify:false`.

!!! warning
    Don't provide a spec for MPI or Compilers, which are configured in the [`mpi:`](recipes.md#mpi) and [`compilers`](recipes.compilers) fields respecively.

!!! warning
    Stackinator does not support "spec matrices", and likely won't, because they use multiple compiler toolchains in a manner that is contrary to the Stackinator "keep it simple" principle.

### Packages

To specify external packages that should be used instead of building them, use the `packages` field.
For example, if the `perl`, `python@3` and `git` packages are build dependencies of an environment and the versions that are available in the base CrayOS installation are sufficient, the following spec would be specified:

```yaml title="environments.yaml: specifying external packages"
my-env:
  packages:
  - perl
  - git
```

!!! note
    If a package is not found, it will be built by Spack.

!!! note
    External packages specified in this manner will only be used when concretising this environment, and will not affect downstream users.

??? note "expand if you are curious how Stackinator configures Spack for packages"
    The following Spack call is used to generate `packages.yaml` in the Spack environment that Stackinator generates in the build path to concretise and build the packages in the example above:

    ```bash title="Makefile target for external packages in an environment"
    packages.yaml:
        spack external find --not-buildable --scope=user perl git
    ```

### Variants

To specify variants that should be applied to all package specs in the environment by default (unless overridden explicitly in a package spec), use the `variants` field.
For example, to concretise all specs in an environment that support MPI or CUDA and target A100 GPUs, the following `variants` could be set:

```yaml title="environments.yaml: variants for MPI and CUDA on A100"
cuda-env:
  variants:
    - +mpi
    - +cuda
    - cuda_arch=80
```

??? note "expand if you are curious how Stackinator configures Spack for variants"
    The above will add the following to the generated `spack.yaml` file used internally by Spack.

    ```yaml title="spack.yaml: packages spec generated for variants"
    spack:
      packages:
        all:
          variants:
          - +mpi
          - +cuda
          - cuda_arch=80
    ```

### Views

File system views are an optional way to provide the software from an environment in a directory structure similar to `/usr/local`, based on Spack's [filesystem views](https://spack.readthedocs.io/en/latest/environments.html#filesystem-views).

Each environment can provide more than one view, and the structure of the YAML is the same as used by the version of Spack used to build the Spack stack.
For example, the `views` description:

```yaml
cuda-env:
  views:
    default:
    no-python:
      exclude:
        - 'python'
```

will configure two views:

* `default`: a view of all the software in the environment using the default settings of Spack.
* `no-python`: everything in the default view, except any versions of `python`.

Stackinator provides some additional options that are not provided by Spack, to fine tune the view, that can be set in the `uenv:` field:

```yaml
cuda-env:
  views:
    uenv:
      add_compilers: true
      prefix_paths:
        LD_LIBRARY_PATH: [lib, lib64]
```

* `add_compilers` (default `true`): by default Spack will not add compilers to the `PATH` variable. Stackinator automatically adds the `gcc` and/or `nvhpc` to path. This option can be used to explicitly disable or enable this feature.
* `prefix_paths` (default empty): this option can be used to customise prefix style environment variables (`PATH`, `LD_LIBRARY_PATH`, `PKG_CONFIG_PATH`, `PYTHONPATH`, etc).
    * the key is the environment variable, and the value is a list of paths to search for in the environment view. All paths that match an entry in the list will be prepended to the prefix path environment variable.
    * the main use for this feature is to opt-in to setting `LD_LIBRARY_PATH`. By default Spack does not add `lib` and `lib64` to `LD_LIBRARY_PATH` because that can break system installed applications that depend on `LD_LIBRARY_PATH` or finding their dependencies in standard locations like `/usr/lib`.

See the [interfaces documentation](interfaces.md#environment-views) for more information about how the environment views are provided to users of a stack.

## Modules

Modules are generated for the installed compilers and packages by spack. The default module generation rules set by the version of spack specified in `config.yaml` will be used if no `modules.yaml` file is provided.

To set rules for module generation, provide a `modules.yaml` file as per the [spack documentation](https://spack.readthedocs.io/en/latest/module_file_support.html).

To disable module generation, set the field `config:modules:False` in `config.yaml`.

## Custom Spack Packages

An optional package repository can be added to a recipe to provide new or customized Spack packages in addition to Spack's `builtin` package repository, if a `repo` path is provided in the recipe.

For example, the following `repo` path will add custom package definitions for the `hdf5` and `nvhpc` packages:

```
repo
└─ packages
   ├─ hdf5
   │  └─ package.py
   └─ nvhpc
      └─ package.py
```

Additional custom packages can be provided as part of the cluster configuration, as well as additional site packages.
These packages are all optional, and will be installed together in a single Spack package repository that is made available to downstream users of the generated uenv stack.
See the documentation for [cluster configuration](cluster-config.md) for more detail.

!!! alps
    All packages are installed under a single spack package repository called `alps`.
    The CSCS configurations in [github.com/eth-cscs/alps-cluster-config](https://github.com/eth-cscs/alps-cluster-config) provides a site configuration that defines cray-mpich, its dependencies, and the most up to date versions of cuda, nvhpc etc to all clusters on Alps.

!!! warning
    Unlike Spack package repositories, any `repos.yaml` file in the `repo` path will be ignored.
    This is because the provided packages are added to the `alps` namespace.

## Post install configuration

If a script `post-install` is provided in the recipe, it will be run during the build process: after the stack has been built, and just before the final squashfs image is generated.
Post install scripts can be used to modify or extend an environment with operations that can't be performed in Spack, for example:

* configure a license file;
* install additional software outside of Spack;
* generate activation scripts.

The following steps are effectively run, where we assume that the recipe is in `$recipe` and the mount point is the default `/user-environment`:

```bash
# copy the post-install script to the mount point
cp "$recipe"/post-install /user-environment
chmod +x /user-environment/post-install

# apply Jinja templates
jinja -d env.json /user-environment/post-install > /user-environment/post-install

# execute the script from the mount point
cd /user-environment
/user-environment/post-install
```

The post-install script is templated using Jinja, with the following variables available for use in a script:

| Variable    | Description                          |
| ----------- | ------------------------------------ |
| `env.mount` | The mount point of the image - default `/user-environment` |
| `env.config`| The installation tree of the Spack installation that was built in previous steps |
| `env.build` | The build path |
| `env.spack` | The location of Spack used to build the software stack (only available during installation) |

The use of Jinja templates is demonstrated in the following example of a bash script that generates an activation script that adds the installation path of GROMACS to the system PATH:

```bash title="post-install script that generates a simple activation script."
#!/bin/bash

gmx_path=$(spack -C {{ env.config }} location -i gromacs)/bin
echo "export PATH=$gmx_path:$PATH" >> {{ env.mount }}/activate.sh
```

!!! note
    The copy of Spack used to build the stack is available in the environment in which `post-install` runs, and can be called directly.

!!! note
    The script does not have to be bash - it can be in any scripting language, such as Python or Perl, that is available on the target system.

## Pre install configuration

Similarly to the post-install hook, if a `pre-install` script is provided in the recipe, it will be run during the build process:

* directly after the initial test that Spack has been installed correctly;
* directly before the build cache is configured, and/or the first compiler environment is concretised.

The pre-install script is copied, templated and executed similarly to the post-install hook (see above).

## Meta-Data

Stackinator generates meta-data about the stack to the `extra` path of the installation path.
A recipe can install arbitrary meta data by providing a `extra` path, the contents of which will be copied to the `meta/extra` path in the installation path.

!!! alps
    This is used to provide additional information required by ReFrame as part of the CI/CD pipeline for software stacks on Alps, defined in the [GitHub eth-cscs/alps-spack-stacks](https://github.com/eth-cscs/alps-spack-stacks) repository.
