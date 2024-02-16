from setuptools import setup, find_packages
import datetime

# Dynamically read in the requirements from requirements.txt
with open('requirements.txt') as f:
    requirements = f.read().splitlines()

# Dynamic versioning based on the current date and time
version = datetime.datetime.now().strftime("%Y.%m.%d.%H%M%S")

setup(
    name='zillowanalyzer',
    version=version,
    packages=find_packages(),
    install_requires=requirements,
    author='Jorge Canedo',
    author_email='jcanedo@g.hmc.edu',
    description='A package forscraping, processing and anlyzing Zillow real estate data.',
    keywords='zillow',
)
