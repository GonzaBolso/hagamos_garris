# Hagamos Garris — HLL Discord Bot

Bot de Discord + Collector para servidores de **Hell Let Loose** con CRCON.  
Recolecta estadísticas de partidas, genera rankings, administra desafíos y muestra el estado del servidor en tiempo real.


## Instalación

### Requisitos

- Docker + Docker Compose
- Servidor CRCON con API key
- Bot de Discord con permisos: Send Messages, Embed Links, Manage Messages

### Pasos

**1. Clonar y configurar**

```bash
git clone <repo>
cd hagamos_garris
cp .env.example .env
# Editar .env con tus valores
```

**2. Crear el bot de Discord**

1. https://discord.com/developers/applications → New Application
2. Bot → Reset Token → copiar `DISCORD_TOKEN`
3. OAuth2 → URL Generator → `bot` + `applications.commands` → invitar al servidor
4. Copiar `GUILD_ID` (ID del servidor, click derecho con Dev Mode activado)

**3. Configurar CRCON**

En CRCON → Settings → API Keys → crear key → copiar en `CRCON_API_KEY`.

**4. Levantar**

```bash
docker compose up -d --build
```

**5. Configurar canales en Discord**

```
/hlladmin setchannel canal:#general-hll
/hlladmin setchannel canal_snapshots:#stats canal_desafios:#desafios
/hlladmin setroles @admin:@Admin @player:@Jugador
```

---

## Comandos

### Jugadores (/hll)

| Comando | Descripción |
|---|---|
| `/hll registro <steam_id>` | Vincula tu Discord con tu Steam/Epic ID |
| `/hll perfil` | Tu perfil: horas, sesiones, nivel, VIP |
| `/hll server` | Estado del servidor: mapa, score, tiempo restante |
| `/hll online` | Jugadores conectados ahora mismo |
| `/hll vip` | Verificá si tenés VIP activo |
| `/hll top <categoria> [periodo]` | Ranking: Kills, K/D, Partidas, Combat, etc. |
| `/hll weapon <arma>` | Top 10 jugadores con más kills con esa arma |
| `/hll desafio listar` | Desafíos activos |
| `/hll desafio progreso` | Ranking de progreso (autocomplete por nombre) |

### Stats (/stats)

| Comando | Descripción |
|---|---|
| `/stats show` | Tus stats acumulados |
| `/stats games [cantidad]` | Tus últimas N partidas |
| `/stats weapon` | Tus kills por arma con ranking global |

### Admin (/hlladmin)

| Comando | Descripción |
|---|---|
| `/hlladmin setchannel` | Configura canales (jugadores, snapshots, desafíos, vinculados, eventos, status) |
| `/hlladmin setroles` | Configura roles de admin y player |
| `/hlladmin config` | Muestra configuración actual |
| `/hlladmin snapshot [periodo]` | Manda el Top 10 manualmente |
| `/hlladmin armas` | Descarga .txt con todos los nombres de armas registrados |
| `/hlladmin desafio metricas` | Lista métricas disponibles |
| `/hlladmin desafio crear` | Crea un desafío manual |
| `/hlladmin desafio eliminar` | Desactiva un desafío |
| `/hlladmin desafio plantilla` | Descarga JSON de ejemplo para importar |
| `/hlladmin desafio importar` | Crea desafíos en lote desde un JSON |

---

## Desafíos

### Períodos

| Período | Descripción |
|---|---|
| `diario` | 00:00 — 23:59 de hoy |
| `semanal` | Lunes a domingo |
| `mensual` | Mes calendario |
| `current_match` | Partida en curso |
| `next_match` | Próxima partida |
| `custom` | Rango de fechas libre |

### Métricas

| Métrica | Parámetro extra |
|---|---|
| `kills`, `deaths`, `kd_ratio`, `matches` | — |
| `combat`, `offense`, `defense`, `support` | — |
| `vehicles_destroyed` | — |
| `kills_weapon` | `arma: "M1 GARAND"` |
| `kills_player` | `steam_id: "76561..."` |
| `kills_type` | `tipo_kill: "infantry"` (ver tipos abajo) |

**Tipos de kill:** `infantry`, `armor`, `machine_gun`, `sniper`, `bazooka`, `grenade`, `mine`, `satchel`, `commander`, `artillery`, `self_propelled_artillery`

### Importar desafíos en lote

```
/hlladmin desafio plantilla  →  descarga plantilla_desafios.json
# editar el archivo
/hlladmin desafio importar archivo:plantilla_desafios.json
```

Ver también `plantilla_desafios.json` en la raíz del repo como referencia.

---

## Panel de estado del servidor

Se activa con `/hlladmin setchannel canal_status:#canal`.  
Actualiza automáticamente cada 60 segundos con: mapa actual/próximo, score, votación, jugadores por equipo con commander.

---

## Snapshot automático

Todos los días a las **23:55 hora UY** manda el Top 10 del día al canal de snapshots.  
Los lunes agrega el semanal. El último día del mes agrega el mensual.

Disparo manual: `/hlladmin snapshot periodo:Día`

---

## Notificaciones de estado

El bot y el collector pueden notificar errores y eventos (conectado/desconectado) a un canal de Discord via webhook.

1. Canal → Editar → Integraciones → Webhooks → Nuevo → copiar URL
2. Agregar al `.env`: `STATUS_WEBHOOK_URL=https://discord.com/api/webhooks/...`

---

## Estructura del proyecto

```
hagamos_garris/
├── collector/
│   ├── main.py       # Loops: collector, live polling, detector de eventos
│   ├── service.py    # Lógica: process_maps, desafíos en vivo
│   ├── db.py         # Queries SQL
│   ├── crcon.py      # Cliente HTTP CRCON
│   └── config.py     # Variables de entorno
├── bot/
│   ├── main.py       # Bootstrap
│   ├── commands/     # hll.py, stats.py, challenges.py
│   ├── services/     # Lógica de negocio
│   ├── db/           # Queries SQL
│   └── api/crcon.py  # Cliente CRCON
├── postgres/
│   └── init.sql      # Schema
├── plantilla_desafios.json
├── .env.example
└── docker-compose.yml
```