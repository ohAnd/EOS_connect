## Contributing

We welcome PRs. Keep main clean, iterate fast on develop.

Branch roles
- main: stable, tagged releases only (comes from develop).
- develop: integration branch (target of normal PRs).
- feature_<short-desc> or feature_<issue>-<desc>: new code (from develop).
- bugfix_<issue>-<desc>: fix for something already in develop.
- hotfix_<issue>-<desc>: urgent production fix (from main → PR to main → merge back into develop).
- issue-<number>-<desc>: automatically created from a GitHub issue (allowed and recommended).

You can create a branch manually or use GitHub’s "Create branch" button on an issue, which will name it like `issue-123-description`. This is fully supported and recommended for traceability.

Flow
1. Update local: git fetch origin && git switch develop && git pull --ff-only
2. Create branch: git switch -c feature/better-forecast
3. Code + tests + docs (README / CONFIG_README / MQTT if behavior changes)
4. Run formatting, lint, tests
   - Ensure all Python files are formatted with [Black](https://black.readthedocs.io/en/stable/) (`black .`)
     - **Tip for VS Code users:** Install the [Black Formatter extension](https://github.com/microsoft/vscode-black-formatter) for automatic formatting on save. (// VS Code settings.json "[python]": { "editor.formatOnSave": true })
   - Run [pylint](https://pylint.pycqa.org/) and ensure a score of **9.0 or higher** for all files (`pylint src/`)
   - tests - see info at guidelines below
5. Rebase before PR: git fetch origin && git rebase origin/develop
6. Push: git push -u origin feature/better-forecast
7. Open PR → base: develop (link issues: Closes #123)
8. Keep PR focused; squash or rebase merge (no merge commits)

Commits (Conventional)
feat: add battery forecast smoothing
fix: correct negative PV handling
docs: update MQTT topic table

Hotfix
git switch main
git pull --ff-only
git switch -c hotfix/overrun-calc
...fix...
PR → main, tag release, then: git switch develop && git merge --ff-only main

Guidelines
- One logical change per PR
- Add/adjust tests for logic changes
  - Use [pytest](https://docs.pytest.org/) for all unit and integration tests.
  - Place tests in the `tests/` directory, organized to mirror the structure of the `src/` directory:
    - Create a subfolder for each source module or feature (e.g., if your code is in `src/interfaces/mqtt_interface.py`, place tests in `tests/interfaces/test_mqtt_interface.py`).
    - Name test files as `test_<uut-filename>.py` (e.g., `test_mqtt_interface.py` for `mqtt_interface.py`).
- Document new config keys / API / MQTT topics
- Prefer clarity over cleverness

Thanks for contributing!