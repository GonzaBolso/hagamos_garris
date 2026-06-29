-- ── Usuarios vinculados Discord <-> Steam ────────────────────
CREATE TABLE IF NOT EXISTS linked_players (
    discord_id      BIGINT PRIMARY KEY,
    steam_id        VARCHAR(64) NOT NULL UNIQUE,
    discord_name    VARCHAR(100),
    linked_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Jugadores conocidos (steam_id + nombre más reciente) ──────
-- Se llena/actualiza automáticamente cada vez que el collector procesa
-- una partida. Se usa para el autocompletado por nombre en
-- /hlladmin desafio crear (parámetro jugador_victima).
CREATE TABLE IF NOT EXISTS players (
    steam_id          VARCHAR(64) PRIMARY KEY,
    player_name       VARCHAR(100),
    last_match_start  TIMESTAMPTZ  -- fecha de la partida más reciente que actualizó este nombre
);

CREATE INDEX IF NOT EXISTS idx_players_name ON players (player_name);

-- ── Partidas procesadas por el collector ──────────────────────
CREATE TABLE IF NOT EXISTS matches (
    match_id        VARCHAR(64) PRIMARY KEY,   -- ID único que da CRCON
    map_name        VARCHAR(100),
    start_time      TIMESTAMPTZ,
    end_time        TIMESTAMPTZ,
    allied_score    INT,
    axis_score      INT,
    processed_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ── Stats por jugador por partida ─────────────────────────────
CREATE TABLE IF NOT EXISTS match_player_stats (
    id              SERIAL PRIMARY KEY,
    match_id        VARCHAR(64) REFERENCES matches(match_id) ON DELETE CASCADE,
    steam_id        VARCHAR(64) NOT NULL,
    player_name     VARCHAR(100),
    kills           INT DEFAULT 0,
    deaths          INT DEFAULT 0,
    teamkills       INT DEFAULT 0,
    combat_score    INT DEFAULT 0,
    offense_score   INT DEFAULT 0,
    defense_score   INT DEFAULT 0,
    support_score   INT DEFAULT 0,
    time_seconds    INT DEFAULT 0,
    UNIQUE (match_id, steam_id)
);

CREATE INDEX IF NOT EXISTS idx_mps_steam ON match_player_stats (steam_id);
CREATE INDEX IF NOT EXISTS idx_mps_match ON match_player_stats (match_id);

-- ── Kills individuales con arma (un kill por fila) ────────────
-- Se llena cuando el collector cierra cada partida, consultando
-- get_historical_logs acotado al rango de esa partida. Excluye TEAM
-- KILL. Se usa para desafíos tipo 'kills_weapon'/'kills_player' y para
-- el Top de armas en /stats show.
CREATE TABLE IF NOT EXISTS kill_events (
    id          SERIAL PRIMARY KEY,
    match_id    VARCHAR(64) REFERENCES matches(match_id) ON DELETE CASCADE,
    event_time  TIMESTAMPTZ,
    killer_id   VARCHAR(64) NOT NULL,
    killer_name VARCHAR(100),
    victim_id   VARCHAR(64) NOT NULL,
    victim_name VARCHAR(100),
    weapon      VARCHAR(150)
);

CREATE INDEX IF NOT EXISTS idx_kill_events_match ON kill_events (match_id);
CREATE INDEX IF NOT EXISTS idx_kill_events_killer ON kill_events (killer_id);
CREATE INDEX IF NOT EXISTS idx_kill_events_killer_weapon ON kill_events (killer_id, weapon);
CREATE INDEX IF NOT EXISTS idx_kill_events_killer_victim ON kill_events (killer_id, victim_id);

-- ── Vista: stats acumulados por jugador (para /hll top) ───────
CREATE OR REPLACE VIEW player_totals AS
SELECT
    mps.steam_id,
    COALESCE(p.player_name, MAX(mps.player_name))::TEXT   AS last_name,
    COUNT(DISTINCT mps.match_id)                    AS matches_played,
    SUM(mps.kills)                                  AS total_kills,
    SUM(mps.deaths)                                 AS total_deaths,
    CASE WHEN SUM(mps.deaths) = 0
         THEN SUM(mps.kills)::FLOAT
         ELSE ROUND((SUM(mps.kills)::NUMERIC / SUM(mps.deaths)), 2)
    END                                             AS kd_ratio,
    SUM(mps.combat_score)                           AS total_combat,
    SUM(mps.offense_score)                          AS total_offense,
    SUM(mps.defense_score)                          AS total_defense,
    SUM(mps.support_score)                          AS total_support,
    SUM(mps.time_seconds)                           AS total_time_seconds
FROM match_player_stats mps
LEFT JOIN players p ON p.steam_id = mps.steam_id
GROUP BY mps.steam_id, p.player_name;

-- ── Configuración del servidor de Discord ─────────────────────
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id                BIGINT PRIMARY KEY,
    stats_channel_id        BIGINT,             -- canal donde los jugadores usan comandos
    snapshot_channel_id     BIGINT,             -- canal de los Top diarios/semanales/mensuales automáticos
    challenge_channel_id    BIGINT,             -- canal donde se manda la foto final al cerrar un desafío
    vinculados_channel_id   BIGINT,             -- canal privado con la lista de vinculados Discord<->Steam
    vinculados_message_id   BIGINT,             -- mensaje fijo que se edita con la lista, en ese canal
    log_channel_id          BIGINT,
    admin_role_id           BIGINT,
    mod_role_id             BIGINT,
    language                VARCHAR(5) DEFAULT 'es',
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ── Sistema de desafíos con múltiples métricas (AND) ──────────
-- Si ya existían las tablas viejas, las recreamos desde cero.
DROP TABLE IF EXISTS challenge_progress CASCADE;
DROP TABLE IF EXISTS challenge_metric_progress CASCADE;
DROP TABLE IF EXISTS challenge_metrics CASCADE;
DROP TABLE IF EXISTS challenges CASCADE;

CREATE TABLE challenges (
    id                          SERIAL PRIMARY KEY,
    guild_id                    BIGINT NOT NULL,
    name                        VARCHAR(150) NOT NULL,
    description                 VARCHAR(500),
    period                      VARCHAR(15) NOT NULL,   -- 'custom' | 'current_match'
    start_date                  TIMESTAMPTZ,            -- fecha de inicio (custom: al crearlo; current_match: cuando arrancó el mapa)
    end_date                    TIMESTAMPTZ,            -- fecha de fin fija, solo para 'custom' (NULL en current_match)
    match_id                    VARCHAR(64),            -- se completa cuando la partida de un current_match cierra
    anchor_map_start            BIGINT,                 -- histórico/sin uso actual; se deja por compatibilidad con desafíos viejos
    map_name                    VARCHAR(150),           -- nombre del mapa que sigue un current_match (ej: 'Utah Beach Warfare')
    map_start                   BIGINT,                 -- timestamp epoch de inicio de ese mapa
    active                      BOOLEAN DEFAULT TRUE,
    notify_ingame               BOOLEAN DEFAULT FALSE,
    pending_close_notification  BOOLEAN DEFAULT FALSE,  -- el collector la prende al cerrar; el bot la apaga tras notificar
    closed_at                   TIMESTAMPTZ,            -- momento exacto del cierre (para descartar notificaciones tras 30 min sin canal configurado)
    created_by                  BIGINT,
    created_at                  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_challenges_active ON challenges (guild_id, active);

-- Cada desafío tiene 1 o más métricas; TODAS deben cumplirse (AND)
CREATE TABLE challenge_metrics (
    id              SERIAL PRIMARY KEY,
    challenge_id    INT REFERENCES challenges(id) ON DELETE CASCADE,
    metric          VARCHAR(30) NOT NULL,   -- 'kills','kd_ratio','matches','combat','offense','defense','support','kills_weapon','kills_player'
    target          NUMERIC NOT NULL,
    param           VARCHAR(150)            -- arma exacta (kills_weapon) o steam_id de víctima (kills_player); NULL para el resto
);

CREATE INDEX idx_metrics_challenge ON challenge_metrics (challenge_id);

-- Progreso de cada jugador POR métrica
CREATE TABLE challenge_metric_progress (
    id                  SERIAL PRIMARY KEY,
    challenge_metric_id INT REFERENCES challenge_metrics(id) ON DELETE CASCADE,
    steam_id            VARCHAR(64) NOT NULL,
    player_name         VARCHAR(100),
    progress            NUMERIC DEFAULT 0,
    completed           BOOLEAN DEFAULT FALSE,
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (challenge_metric_id, steam_id)
);

CREATE INDEX idx_metric_progress ON challenge_metric_progress (challenge_metric_id, steam_id);

-- Estado consolidado por jugador y desafío (completed = TRUE solo si TODAS las métricas lo están)
CREATE TABLE challenge_progress (
    id              SERIAL PRIMARY KEY,
    challenge_id    INT REFERENCES challenges(id) ON DELETE CASCADE,
    steam_id        VARCHAR(64) NOT NULL,
    player_name     VARCHAR(100),
    completed       BOOLEAN DEFAULT FALSE,
    completed_at    TIMESTAMPTZ,
    notified        BOOLEAN DEFAULT FALSE,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (challenge_id, steam_id)
);

CREATE INDEX idx_progress_challenge ON challenge_progress (challenge_id);