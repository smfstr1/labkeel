# Contributing to LabKeel

Thanks for helping. Bug reports, ideas and pull requests are all welcome.

## Reporting a bug or suggesting something

Open an issue and pick **Something isn't working** or **An idea**. For anything that could affect someone's
security, use the private route in [SECURITY.md](SECURITY.md) instead.

## Working on the code

```sh
git clone https://github.com/smfstr1/labkeel.git && cd labkeel
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest              # the tests (they never touch the real machine)
python -m pyflakes app tests  # unused or undefined names
```

[docs/guide.md](docs/guide.md#working-on-the-code) has a map of the code. A few habits keep it easy to work on:

- **Write for the person reading it.** Each Python file starts by saying what it does, each function says what
  it's for, and messages people see are plain British English.
- **Powerful things start off.** Anything that can change the machine is admin-only, off until turned on in
  `docker-compose.yml`, asks for the password again, and is written to the activity log.
- **Test what you change.** Add a test next to the ones for that part (`tests/test_<part>.py`). Tests run
  against a throwaway folder with commands on the machine switched off, and the Docker ones skip themselves when
  Docker isn't there.
- **Front end:** the dashboard is plain HTML, CSS and JavaScript in `app/static/`, with no build step. Format
  it with `npx prettier@3 --write "app/static/ui/*.{js,css}"`, and check pages on a phone-sized screen as
  well as a desktop one.
- **New settings** go in `app/env.py` and [docs/settings.md](docs/settings.md), with a `LABKEEL_` name that
  says what they do.

## Releasing

1. Change `VERSION` and add the changes to `CHANGELOG.md`.
2. Commit, then tag that commit `v` plus the version (`v0.1.2-alpha`). CI checks the tag matches `VERSION`.
