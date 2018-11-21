from setuptools import setup


with open('requirements.txt') as f:
    requirements = f.read().splitlines()

setup(name='bbmigrate',
      version=1.0,
      description="bitbucket-issue-migration",
      classifiers=[
          'Development Status :: 3 - Alpha',
          'Environment :: Console',
          'Programming Language :: Python',
          'Programming Language :: Python :: Implementation :: CPython',
      ],
      author="Jeff Widman, Vitaly Babiy, Mike Bayer",
      url='https://github.org/zzzeek/bitbucket-issue-migration',
      license='GPL',
      packages=["bbmigrate"],
      zip_safe=False,
      install_requires=requirements,
      entry_points={
          'console_scripts': [
              'bbmigrate = bbmigrate.main:main',
          ],
      })
