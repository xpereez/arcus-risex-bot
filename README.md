# Bot Control Center / Arcus + Lighter

Motor de ciclos que ejecuta primero una orden maker en Arcus y cubre en Lighter cada incremento confirmado de fill. El cierre repite la secuencia en sentido inverso. Incluye persistencia SQLite y un dashboard general para supervisar varios bots.

El dashboard incorpora actualmente:

- `Arcus + Lighter`: ejecución delta neutral, wallets, volúmenes, posiciones y ciclos.
- `Market Making Lighter`: proceso shadow supervisado, recorder, última quote teórica, lifecycle de quotes, fills simulados, markouts y continuidad del libro.

La vista de market making incorpora análisis automático de 24/72 horas: actividad por hora, capture y markout a 5 segundos, volumen, churn de quotes, resultados segmentados por lado, volatilidad, basis y spread cotizado, además de alertas y recomendaciones basadas en la muestra. El cálculo se sirve por `/api/bots/market-making-lighter/analytics` y se refresca cada minuto sin bloquear la telemetría en tiempo real.

El dashboard arranca y detiene el segundo bot como proceso independiente, pero fuerza siempre `MM_MODE=shadow`: no existe una ruta de envío de órdenes. La base se lee desde `../marketMakingLighter/data/market_maker.sqlite3`; las ubicaciones pueden cambiarse con `MARKET_MAKING_LIGHTER_PROJECT_PATH` y `MARKET_MAKING_LIGHTER_DATABASE_PATH`.

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

El control de arranque de cada motor es independiente:

```dotenv
# Arcus puede permanecer detenido aunque el dashboard arranque con el sistema.
BOT_AUTO_START=false

# Arranca únicamente el recorder/market maker shadow.
MARKET_MAKING_AUTO_START=true
```

Los botones **Detener** e **Iniciar shadow** del panel de Market Making actúan sólo sobre ese proceso. Una parada cancela las quotes hipotéticas, persiste el estado `STOPPED` y elimina el PID; un arranque crea una sesión nueva y recupera los libros públicos.

## Inicio automático en macOS

`deploy/com.xpereez.bot-control.plist` es el LaunchAgent de esta instalación. Mantiene disponible el dashboard después de iniciar sesión y éste, a su vez, inicia el market maker shadow. La configuración local conserva `BOT_AUTO_START=false`, por lo que Arcus no empieza a operar por este mecanismo.

```bash
cp deploy/com.xpereez.bot-control.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.xpereez.bot-control.plist
```

Los logs del servicio se escriben en `data/dashboard-launchd.log` y los del market maker en `../marketMakingLighter/data/market_maker.log`.

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
