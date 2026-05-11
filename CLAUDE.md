# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Instaloader** is a Python library that downloads Instagram content (profiles, stories, hashtags, feeds) along with metadata (captions, comments, geotags). It provides both a CLI tool and a programmatic Python API.

- **Status**: Production-stable (v4.15.1)
- **Python**: 3.9+
- **Package manager**: pipenv (Pipfile)

## Quick Start

### Setup
```bash
# Install dependencies via pipenv
pipenv install --dev

# Activate the virtual environment
pipenv shell
```

### Running Tests
```bash
# Run all unit tests
python -m pytest test/instaloader_unittests.py -v

# Run a specific test
python -m pytest test/instaloader_unittests.py::TestInstaloaderAnonymously::test_name -v
```

### Code Quality
```bash
# Run pylint (configured in .pylintrc)
pylint instaloader/

# Run mypy for type checking
mypy instaloader/

# Run both linters
pipenv run pylint instaloader/ && pipenv run mypy instaloader/
```

### Documentation
```bash
# Build Sphinx documentation
cd docs
pip install -r requirements.txt
make html  # Output in docs/_build/html/

# View locally: open docs/_build/html/index.html
```

### CLI Usage
```bash
# Install package in development mode
pip install -e .

# Run as command
instaloader profile [profile ...]

# Run via module
python -m instaloader profile [profile ...]
```

## Architecture

### Core Components

**[instaloader/instaloader.py](instaloader/instaloader.py) (1669 lines)**
- Main `Instaloader` class: orchestrates downloads, handles session management, provides API
- Key methods: `download_profile()`, `download_hashtag()`, `download_stories()`, `graphql_query()`, `get_json()`
- Manages file I/O, download loops, resumable iteration

**[instaloader/structures.py](instaloader/structures.py) (2307 lines)**
- Data structures: `Profile`, `Post`, `Story`, `Hashtag`, `Highlight`, `PostComment`
- Each structure maps to Instagram API responses (GraphQL JSON)
- Provides serialization/deserialization: `load_structure_from_file()`, `save_structure_to_file()`
- Format strings for flexible filename/directory naming (e.g., `{date} {caption}`)

**[instaloader/instaloadercontext.py](instaloader/instaloadercontext.py) (885 lines)**
- `InstaloaderContext`: encapsulates HTTP session, rate limiting, error handling
- `RateController`: implements Instagram API rate limiting
- Handles authentication (session cookies), user agent, proxy support
- Network error recovery and retry logic

**[instaloader/__main__.py](instaloader/__main__.py) (616 lines)**
- CLI argument parsing and entry point
- Command-line interface for all download operations
- Maps CLI arguments to Instaloader API calls

**[instaloader/nodeiterator.py](instaloader/nodeiterator.py) (329 lines)**
- Iterators for paginated GraphQL responses
- `NodeIterator`: resumable iteration with checkpoint support
- Handles GraphQL cursor-based pagination

**[instaloader/exceptions.py](instaloader/exceptions.py) (84 lines)**
- Exception hierarchy: `InstaloaderException`, `LoginException`, `BadCredentialsException`, etc.

**[instaloader/lateststamps.py](instaloader/lateststamps.py) (126 lines)**
- Persistent storage of last-download timestamps per profile
- Enables incremental updates without re-downloading all content

### Data Flow

1. **User initiates download** via CLI or Python API
2. **Instaloader** authenticates (session cookies) via InstaloaderContext
3. **GraphQL queries** fetch Instagram metadata (Profile, Posts, Stories)
4. **Structures** deserialize API responses into Python objects
5. **File I/O** saves media and metadata using format strings
6. **RateController** enforces Instagram's rate limits

### Key Design Patterns

- **Decorator pattern**: `@_requires_login`, `@_retry_on_connection_error` for cross-cutting concerns
- **Iterator pattern**: Resumable iteration for large downloads with checkpoint support
- **Builder pattern**: CLI argument parsing constructs Instaloader instances
- **Format string substitution**: Flexible output directory/filename generation via Python `string.Formatter`

## Common Development Tasks

### Adding a New Download Feature
1. Add API call to `InstaloaderContext` (network layer)
2. Define data structure in `structures.py`
3. Add download method to `Instaloader` class
4. Add CLI argument in `__main__.py`
5. Add unit test in `test/instaloader_unittests.py`

### Fixing an Instagram API Issue
- Instagram frequently changes GraphQL schema and endpoints
- Check `instaloader.py:graphql_query()` and `get_json()` for request/response handling
- API changes often break field extraction in `structures.py` (watch for `KeyError`)
- Update tests with sample API responses in `test/` (may require Instagram session)

### Modifying Download Logic
- Download state managed in `Instaloader` class
- Resumable iteration via `NodeIterator` and checkpoints
- File naming controlled by format strings in `structures.py` and CLI arguments

## Testing Notes

- Tests use `unittest.TestCase` (not pytest, though pytest can run them)
- Tests are integration-style: require real Instagram API calls (use public profiles like `selenagomez`)
- Rate limiting enforced per request, so test suite runs slowly
- Tests manage temporary directories for file I/O validation
- Global `ratecontroller` variable persists between tests to avoid rate limits

## Important Files

- `.pylintrc`: Pylint configuration (ignore list, score thresholds)
- `Pipfile`, `Pipfile.lock`: Dependency pinning
- `setup.py`: Package metadata, entry points, version management
- `README.rst`: User-facing documentation
- `docs/conf.py`: Sphinx configuration

## Version Management

Version is stored in [instaloader/__init__.py](instaloader/__init__.py) (`__version__` string). Update this before releases; `setup.py` dynamically reads it.

## Deployment

Instaloader is distributed via PyPI. The `setup.py` defines the console script entry point (`instaloader` command).
