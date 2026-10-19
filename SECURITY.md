# Security policy

## Reporting a vulnerability

Please report security issues **privately** through GitHub:
[**Report a vulnerability**](https://github.com/adamtty/ds4-menu/security/advisories/new).
Don't open a public issue for them.

Include what you found, how to reproduce it, and what an attacker could do with it. You'll get a
reply within a week. Once it's fixed, you'll be credited in the advisory unless you'd rather not be.

## Supported versions

Only the latest commit on `main` is supported; there are no release branches.

## Scope

In scope: the proxy (`ds4proxy.py`), the menu bar app, `install.sh` and the build, especially
anything that lets another local process or a web page use the app's Documents permission, load
the model, or redirect its file writes. See [Security](README.md#security) for the protections
already in place.

Out of scope: vulnerabilities in [ds4](https://github.com/antirez/ds4) itself (report those
upstream), and attacks that need an attacker who can already read your Documents folder.
