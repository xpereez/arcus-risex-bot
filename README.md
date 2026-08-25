# Bot Control Center / Arcus + Lighter

Motor de ciclos que ejecuta primero una orden maker en Arcus y cubre en Lighter cada incremento confirmado de fill. El cierre repite la secuencia en sentido inverso. Incluye persistencia SQLite y un dashboard general para supervisar varios bots.

El dashboard incorpora actualmente:

- `Arcus + Lighter`: ejecución delta neutral, wallets, volúmenes, posiciones y ciclos.
- `Market Making Lighter`: estado shadow, recorder, última quote teórica, fills simulados, markouts y continuidad del libro.

El segundo bot se lee en modo read-only desde `../marketMakingLighter/data/market_maker.sqlite3`. La ubicación puede cambiarse con `MARKET_MAKING_LIGHTER_DATABASE_PATH`.

## Estado

- `paper`: funcional y activado por defecto.
- Arcus live: cliente REST y firma Ed25519 implementados a partir del contrato que usa la app pública. Debe validarse con una API key de testnet antes de mainnet.
- Lighter live: integrado con `lighter-sdk` y el endpoint de Robinhood Chain.
- Live permanece bloqueado hasta completar explícitamente las dos variables de seguridad.

## Arranque

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -e '.[test]'
cp .env.example .env
.venv/bin/arcus-lighter-bot
```

Dashboard: `http://127.0.0.1:8787`

En paper, `BOT_PAPER_TIME_SCALE=60` convierte los 20–40 minutos en 20–40 segundos y los 5–10 minutos en 5–10 segundos. Los valores mostrados en configuración siguen siendo los tiempos reales de estrategia.

## Reglas de ejecución

1. Selecciona mercado, dirección y notional dentro de los rangos configurados.
2. Coloca `ALO`/post-only en Arcus al mejor bid o ask.
3. Por cada aumento de `filledSize`, envía en Lighter una orden de mercado opuesta y del mismo size.
4. Cancela la orden Arcus al alcanzar el timeout. Si hubo fill parcial, conserva únicamente ese size cubierto.
5. Mantiene la posición durante un tiempo aleatorio.
6. Cierra reduce-only en Arcus y cubre cada fill con un market reduce-only en Lighter.
7. Si no logra cerrar todo, vuelve a cotizar maker. Si falla el hedge o existe discrepancia, pausa el motor.

La orden de pausa acorta el periodo de mantenimiento y cierra el ciclo actual antes de detener el motor. Al arrancar, el preflight exige que ambas cuentas estén planas; si detecta una posición previa, no abre otro ciclo y muestra el error para reconciliación manual.

## Activación live

Configura credenciales de trading dedicadas y de mínimo privilegio en `.env`. No uses una private key de la wallet principal.

```dotenv
BOT_MODE=live
ENABLE_LIVE_TRADING=true
LIVE_TRADING_ACK=I_UNDERSTAND_THIS_SENDS_REAL_ORDERS
```

Antes de mainnet se deben validar en testnet: creación/cancelación de API key Arcus, forma exacta de la respuesta de órdenes, carrera fill-vs-cancel, redondeos por mercado, autenticación Lighter Robinhood, `reduce_only` y recuperación tras reinicio.

## Pruebas

```bash
.venv/bin/pytest -q
```
