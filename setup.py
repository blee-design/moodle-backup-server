#!/usr/bin/env python3
"""
setup.py for Moodle Backup/Restore Tool
"""

import os
from setuptools import setup

# Read the long description from README.md if it exists
this_directory = os.path.abspath(os.path.dirname(__file__))
readme_path = os.path.join(this_directory, 'README.md')
long_description = ''
if os.path.isfile(readme_path):
    with open(readme_path, encoding='utf-8') as f:
        long_description = f.read()

setup(
    name='moodle_backup_server',
    version='1.0.0',
    description='Backup and restore system for Moodle exam and nrb servers',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='Udaya Raj Joshi',
    author_email='udayarajjoshi@aol.com',
    url='https://github.com/udayarajjoshi/moodle-backup',  # Adjust as needed
    py_modules=['backup_server'],          # Module name (without .py)
    entry_points={
        'console_scripts': [
            'backup-server = backup_server:main',   # Creates the command
        ],
    },
    install_requires=[],
    python_requires='>=3.6',
    license='MIT',
    classifiers=[
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'License :: OSI Approved :: MIT License',
        'Operating System :: POSIX :: Linux',
        'Environment :: Console',
        'Topic :: System :: Archiving :: Backup',
    ],
)
