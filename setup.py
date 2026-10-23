#!/usr/bin/env python3
"""
setup.py for Moodle Backup Server
"""

import os
from setuptools import setup

# --- read README for long_description ---
this_directory = os.path.abspath(os.path.dirname(__file__))
readme_path = os.path.join(this_directory, 'README.md')
long_description = ''
if os.path.isfile(readme_path):
    with open(readme_path, encoding='utf-8') as f:
        long_description = f.read()

setup(
    name='moodle_backup_server',
    version='2.2.0',  # keep whatever `make version` reports
    description='Config-driven backup, restore, and upgrade tool for Moodle servers',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='Udaya Raj Joshi',
    author_email='udayarajjoshi@aol.com',
    url='https://github.com/blee-design/moodle-backup-server',
    project_urls={
        'Source':   'https://github.com/blee-design/moodle-backup-server',
        'Issues':   'https://github.com/blee-design/moodle-backup-server/issues',
        'Changelog': 'https://github.com/blee-design/moodle-backup-server/blob/main/CHANGELOG.md',
    },

    # --- packaging ---
    py_modules=['backup_server'],
    entry_points={
        'console_scripts': [
            'backup-server = backup_server:main',
        ],
    },

    # --- runtime dependencies ---
    # Zero. Everything here is stdlib; system commands (mariadb, tar, xz,
    # php, git) are documented below and checked at runtime by
    # check_required_commands().
    install_requires=[],

    # --- optional dependencies ---
    extras_require={
        # pip install moodle_backup_server[dev]  — full toolchain
        'dev': [
            'build>=1.0',
            'twine>=5.0',
            'pytest>=8.0',
            'ruff>=0.5',
        ],
        # pip install moodle_backup_server[release]  — needed for `make release`
        'release': [
            'build>=1.0',
            'twine>=5.0',
        ],
        # pip install moodle_backup_server[test]  — needed for `make test`
        'test': [
            'pytest>=8.0',
        ],
        # pip install moodle_backup_server[lint]  — needed for `make lint`
        'lint': [
            'ruff>=0.5',
        ],
    },

    # --- runtime requirements ---
    python_requires='>=3.8',   # 3.6/3.7 are EOL; subprocess.run(capture_output=)
                                # needs 3.7+, shutil.copytree(dirs_exist_ok=) needs 3.8+

    license='MIT',
    keywords='moodle backup restore mariadb mysql sysadmin cli',

    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: MIT License',
        'Operating System :: POSIX :: Linux',
        'Environment :: Console',
        'Topic :: System :: Archiving :: Backup',
        'Topic :: Education',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        'Natural Language :: English',
    ],
)
