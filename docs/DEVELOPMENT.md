# Development

## Runtime

Python 3.11.

Canonical interpreter:

$RootFwd/.venv/Scripts/python.exe

## Environment

Create the local virtual environment:

`powershell
py -3.11 -m venv .venv
`

## Verification

Use applicable checks supported by the repository:

`powershell
E:/PythonProject/Scripts/Hermes-sentinel/.venv/Scripts/python.exe -m pytest -q
E:/PythonProject/Scripts/Hermes-sentinel/.venv/Scripts/python.exe -m compileall src
E:/PythonProject/Scripts/Hermes-sentinel/.venv/Scripts/python.exe -m ruff check .
E:/PythonProject/Scripts/Hermes-sentinel/.venv/Scripts/python.exe -m mypy .
E:/PythonProject/Scripts/Hermes-sentinel/.venv/Scripts/python.exe -m pip check
`

Do not invent checks that are not configured by the project.
