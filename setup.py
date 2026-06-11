"""Build script for the miniredis._rdb C extension.

All package metadata lives in pyproject.toml. This file exists solely because
setuptools' declarative configuration does not support C extension declarations
via pyproject.toml -- `ext_modules` must be passed in code. Everything else
(project name, version, dependencies, entry points) stays in pyproject.toml.
"""
from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension(
            name="miniredis._rdb",
            sources=["src/miniredis/_rdb.c"],
            extra_compile_args=[
                "-O2",
                "-Wall",
                "-Wextra",
                "-Wno-unused-parameter",  # CPython API signatures have
                                          # required-but-unused `self`/`args`
                "-std=c11",
            ],
            language="c",
        ),
    ],
)
