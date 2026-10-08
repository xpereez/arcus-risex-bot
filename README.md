# Arcus + RiseX Delta Neutral Bot

Motor de ciclos delta neutral. Abre una posición maker en Arcus, cubre cada fill confirmado en RiseX y cierra las dos patas en sentido inverso. El motor conserva el control por ciclos, la persistencia SQLite y el dashboard del repositorio original, pero la pata de cobertura live usa RiseX.

## Estado actual

- `paper` funciona sin credenciales y sirve para validar el flujo del motor.
- Arcus live conserva su cliente REST y firma Ed25519.
- RiseX live usa `risex==0.10.0`, `RiseXClient.from_signer` para órdenes y `RiseXInfoClient` para mercados, libro, posiciones y saldo.
- No se ha enviado ninguna orden real desde esta integración. Antes de activar mainnet hay que validarla en testnet con tamaños mínimos y una cuenta dedicada.

## Instalación en otro servidor

```bash
git clone https://github.com/xpereez/arcus-risex-bot.git
cd arcus-risex-bot
python3.12 -m venv .venv
.venv/bin/pip install -e '.[test]'
cp .env.example .env
```

Edita `.env`. Para la primera puesta en marcha usa `paper`:

```dotenv
BOT_MODE=paper
BOT_AUTO_START=false
BOT_MARKETS=BTC
```

Arranca el servicio y abre `http://127.0.0.1:8787`:

```bash
.venv/bin/arcus-risex-bot
```

## Credenciales live

La cuenta de RiseX debe tener margen suficiente para la posición hedge. Usa un signer delegado dedicado al bot y limita su saldo. El flujo recomendado es crear el signer con la wallet principal una sola vez y guardar en el servidor únicamente la dirección de la cuenta y la clave del signer que devuelve RiseX. No copies al servidor la private key de la wallet principal.

```dotenv
BOT_MODE=live
ENABLE_LIVE_TRADING=true
LIVE_TRADING_ACK=I_UNDERSTAND_THIS_SENDS_REAL_ORDERS

ARCUS_ADDRESS=0x...
ARCUS_API_KEY=...
ARCUS_API_PRIVATE_KEY=...

RISEX_NETWORK=testnet
RISEX_API_URL=https://api.testnet.rise.trade
RISEX_ACCOUNT_ADDRESS=0x...
RISEX_SIGNER_PRIVATE_KEY=0x...
```

Para mainnet cambia `RISEX_NETWORK=mainnet` y `RISEX_API_URL=https://api.rise.trade`. Comprueba que `BOT_MARKETS` contiene únicamente símbolos presentes en las dos cuentas y que `BOT_MIN_NOTIONAL_USD` respeta los mínimos de ambos mercados.

## Servicio systemd

Ejemplo para `/opt/arcus-risex-bot` y el usuario `arcusbot`:

```ini
[Unit]
Description=Arcus RiseX delta-neutral bot
After=network-online.target
Wants=network-online.target

[Service]
User=arcusbot
WorkingDirectory=/opt/arcus-risex-bot
EnvironmentFile=/etc/arcus-risex-bot.env
ExecStart=/opt/arcus-risex-bot/.venv/bin/arcus-risex-bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Guárdalo como `/etc/systemd/system/arcus-risex-bot.service` y activa el servicio:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now arcus-risex-bot
sudo journalctl -u arcus-risex-bot -f
```

Mantén el fichero de variables fuera del repositorio y protégelo con permisos `600`.

## Flujo de ejecución

1. Carga mercados y comprueba que Arcus y RiseX tienen el símbolo configurado.
2. Exige cuentas planas antes de iniciar ciclos nuevos.
3. Coloca una orden limit post-only en Arcus.
4. Por cada incremento de fill en Arcus envía en RiseX una orden IOC cruzada opuesta del mismo tamaño, limitada por `BOT_MAX_SLIPPAGE_BPS`.
5. Cancela la orden Arcus al alcanzar el timeout y conserva sólo el tamaño que quedó cubierto.
6. Mantiene la posición durante el intervalo configurado.
7. Cierra reduce-only en Arcus y cubre cada fill con una orden reduce-only en RiseX.
8. Detiene el motor si el hedge queda incompleto, aparece delta residual o se alcanza el límite de pérdida diaria.

## Validación pendiente

Antes de mainnet valida en testnet la creación del signer delegado, lectura de mercados y libro, redondeos de `step_size` y `tick_size`, límites de precio de las órdenes market, cancelación, fills parciales, `reduce_only`, liquidación de posiciones y recuperación tras reinicio. La integración live está preparada para esas comprobaciones, pero no se ha podido ejecutar contra una cuenta RiseX en esta sesión.

## Pruebas locales

```bash
.venv/bin/pytest -q
```

La documentación oficial de RiseX está en [developer.rise.trade](https://developer.rise.trade/) y el SDK utilizado por este proyecto en [PyPI](https://pypi.org/project/risex/).
