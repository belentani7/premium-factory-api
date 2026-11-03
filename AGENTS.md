# AGENTS.md — belentani7/premium-factory-api

Instrucciones para agentes de código (Cursor, Claude Code, Codex, Gemini CLI).

## Stacks: python

## Ciclo de verificación

- `python -m unittest discover`

## Reglas

- Evidence over claims: ejecuta la verificación antes de declarar éxito.
- Cambios mínimos y reversibles; un fix llega con su test.
- Nunca commitear secrets ni `.env`.

Ver estándares completos en `.cursor/rules/project.mdc`.
