# premium-factory-api

<p><img src="https://github.com/belentani7/premium-factory-api/actions/workflows/ci.yml/badge.svg" alt="CI" style="vertical-align:middle">
  <img src="https://github.com/belentani7/premium-factory-api/actions/workflows/codeql.yml/badge.svg" alt="CodeQL" style="vertical-align:middle">
  <img src="https://img.shields.io/badge/dependabot-enabled-025e8c?logo=dependabot&style=flat-square" alt="Dependabot"></p>

Proyecto de negocio del ecosistema Belentani.

## Qué hace
Proyecto de negocio del ecosistema Belentani. Repositorio del ecosistema Belentani (categoría: negocio).

## Utilidad
- Utilidad social: Facilita el acceso y la gestión de recursos del ecosistema Belentani para su comunidad.
- Utilidad comercial: Soporta servicios y productos del ecosistema bajo licencia MIT, reutilizables comercialmente.

## Stack
- HTML/CSS/JS estático

## Instalación
Clona el repositorio e instala las dependencias según el lenguaje del proyecto.

```bash
git clone https://github.com/belentani7/premium-factory-api.git
cd premium-factory-api
```

## Estructura

Árbol breve de directorios principales del repositorio.

## Licencia
MIT. Ver archivo `LICENSE`.


## Producto
Véase `plataforma/OFERTA.md`, `plataforma/precios.json`, `plataforma/ONEPAGER.md`.

## Que es

Plantilla de servicio del ecosistema Belentani: HTML/CSS/JS estático con CI, CodeQL y Dependabot ya configurados.

## Como se ejecuta

```bash
python -m http.server 8080
```

Abre `http://127.0.0.1:8080/`. Sin build: HTML estatico.

## Verificacion

```bash
node --test tests/
```

El test de humo comprueba que el arbol esperado existe y que el HTML o el paquete no esta roto.

## Estructura

```
404.html
AGENTS.md
BIBLIA.md
CODEX-IMPLEMENT-WEB.txt
CODE_OF_CONDUCT.md
CONTRIBUTING.md
LICENSE
README.md
accesibilidad.html
brand
config.example.json
data
```

## Entorno

- Node.js 20+ o Python 3.11+ segun el stack detectado.
- Windows 10/11, PowerShell 5.1 o bash.

## Licencia

MIT. Ver `LICENSE`.
