# Estado de sesión

Punto de reanudación guardado el **2026-08-25 14:57 CEST**.

## Repositorios

- `arcus-lighter-bot`: commit publicado `b9102a3` (`Add shadow performance analytics`).
- `marketMakingLighter`: commit publicado `be2907d` (`Add persistent shadow telemetry`).
- Ambos repositorios estaban limpios antes de crear este documento.

## Estado operativo

- Dashboard general: `http://127.0.0.1:8787`.
- LaunchAgent `com.xpereez.bot-control`: instalado y supervisando el dashboard.
- Market Making Lighter: `SHADOW`, proceso activo, libros Robinhood y referencia válidos, `0` gaps de continuidad y `sends_orders=false`.
- Delta Neutral Arcus + Lighter: `STOPPED`, no acepta ciclos, `0` posiciones y `BOT_AUTO_START=false`.
- No arrancar el DN live sin una petición explícita del usuario.
- No añadir ejecución real al market maker; debe continuar forzado a `MM_MODE=shadow`.

## Analítica shadow al guardar

Ventana de 24 horas:

- Sesiones: `5`.
- Fills hipotéticos: `5`.
- Volumen hipotético: `394,41 USD`.
- Acciones de quotes: `23.350`.
- Fill rate: `0,0214 %`.
- Spread capturado medio: `+4,42 bps`.
- Markout medio a 5 segundos: `-1,13 bps`.
- Churn: aproximadamente `397 acciones/min`.
- Alertas: muestra temprana, churn alto y markout negativo a 5 segundos.
- No ajustar parámetros hasta disponer de al menos `30` fills shadow.

Estas métricas cambian mientras el recorder permanece activo. Consultar de nuevo el endpoint `/api/bots/market-making-lighter/analytics?hours=24` al retomar.

## Uso de disco medido

- `arcus-lighter-bot`: `176.904 KiB` totales (~173 MiB); `420 KiB` de datos.
- `marketMakingLighter`: `294.204 KiB` totales (~287 MiB); `152.876 KiB` de datos (~149 MiB).
- Sin retención, el recorder shadow proyecta aproximadamente `12–13 GB` por semana y `0,6–0,7 TB` por año al ritmo observado.

## Siguiente tarea acordada

Implementar almacenamiento acotado sin detener la recogida shadow:

1. Crear rollups horarios persistentes antes de eliminar datos crudos.
2. Conservar `events`, `decisions` y `quote_actions` crudos durante 72 horas.
3. Conservar permanentemente `fills`, `markouts` y `sessions`.
4. Ejecutar mantenimiento cada hora y hacer checkpoint de WAL para reutilizar páginas libres.
5. Mantener los análisis de 24/72 horas compatibles con la retención.
6. Desactivar el access log HTTP repetitivo o rotarlo.
7. Rotar el log del market maker y conservar una ventana pequeña.
8. Añadir métricas de almacenamiento y última limpieza al dashboard.
9. Probar migración, pruning, rollups, replay y recuperación tras reinicio.

Objetivo esperado: estabilizar el uso de disco del market maker alrededor de `5–6 GB`, manteniendo métricas agregadas de largo plazo.

## Validación ya completada

- Dashboard/supervisor/analytics: `24` tests superados.
- Market maker: `14` tests superados.
- Dashboard validado visualmente en escritorio y móvil, sin overflow horizontal.
- Selectores de 24/72 horas, gráficos, segmentación y alertas operativos.
