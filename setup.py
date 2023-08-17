#!/usr/bin/env python

from setuptools import find_packages, setup

# get version from file
__version__ = '0.0.0'
exec(open('schpf/_version.py').read())

requires = ['scikit-learn',
            "numba >= 0.39, !=0.41, !=0.42, !=0.43; python_version<='3.7.3'",
            "numba >= 0.44; python_version=='3.7.4'",
            "numba >= 0.45; python_version>'3.7.4'",
            'scipy >= 1.1',
            'numpy',
            'pandas',
            'joblib'
            ]

tests_require = ['pytest']
extras_require = {
        'loompy' : ['loompy'],
        'docs' : ['sphinx-argparse'],
        }

setup(
    name='sccHPF',
    version=__version__,
    packages=find_packages(),
    scripts=['bin/sccHPF'],
    python_requires='>=3.6',
    install_requires=requires,
    tests_require=tests_require,
    extras_require=extras_require,
    author = 'Nicholas Hou',
    author_email = 'nh2642@columbia.edu',
    description='Single-cell Consensus Hierarchical Poisson Factorization',
    license="N/A",
    url=https://github.com/nicholashou27/sccHPF',
)
