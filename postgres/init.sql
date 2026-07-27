-- ── Usuarios vinculados Discord <-> Steam ────────────────────
CREATE TABLE IF NOT EXISTS linked_players (
    discord_id      BIGINT PRIMARY KEY,
    steam_id        VARCHAR(64) NOT NULL UNIQUE,
    discord_name    VARCHAR(100),
    linked_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Jugadores conocidos ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS players (
    steam_id          VARCHAR(64) PRIMARY KEY,
    player_name       VARCHAR(100),
    last_match_start  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_players_name ON players (player_name);

-- ── Partidas procesadas ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS matches (
    match_id        VARCHAR(64) PRIMARY KEY,
    map_name        VARCHAR(100),
    start_time      TIMESTAMPTZ,
    end_time        TIMESTAMPTZ,
    allied_score    INT,
    axis_score      INT,
    processed_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ── Stats por jugador por partida ─────────────────────────────
CREATE TABLE IF NOT EXISTS match_player_stats (
    id                  SERIAL PRIMARY KEY,
    match_id            VARCHAR(64) REFERENCES matches(match_id) ON DELETE CASCADE,
    steam_id            VARCHAR(64) NOT NULL,
    player_name         VARCHAR(100),
    kills               INT DEFAULT 0,
    deaths              INT DEFAULT 0,
    teamkills           INT DEFAULT 0,
    combat_score        INT DEFAULT 0,
    offense_score       INT DEFAULT 0,
    defense_score       INT DEFAULT 0,
    support_score       INT DEFAULT 0,
    time_seconds        INT DEFAULT 0,
    vehicles_destroyed  INT DEFAULT 0,
    kills_by_type       JSONB DEFAULT '{}',
    deaths_by_type      JSONB DEFAULT '{}',
    weapons             JSONB DEFAULT '{}',
    death_by_weapons    JSONB DEFAULT '{}',
    most_killed         JSONB DEFAULT '{}',
    death_by            JSONB DEFAULT '{}',
    most_killed_ids     JSONB DEFAULT '{}',
    death_by_ids        JSONB DEFAULT '{}',
    UNIQUE (match_id, steam_id)
);

CREATE INDEX IF NOT EXISTS idx_mps_steam ON match_player_stats (steam_id);
CREATE INDEX IF NOT EXISTS idx_mps_match ON match_player_stats (match_id);

-- Migraciones para BDs existentes
ALTER TABLE match_player_stats ADD COLUMN IF NOT EXISTS kills_by_type    JSONB DEFAULT '{}';
ALTER TABLE match_player_stats ADD COLUMN IF NOT EXISTS deaths_by_type   JSONB DEFAULT '{}';
ALTER TABLE match_player_stats ADD COLUMN IF NOT EXISTS weapons          JSONB DEFAULT '{}';
ALTER TABLE match_player_stats ADD COLUMN IF NOT EXISTS death_by_weapons JSONB DEFAULT '{}';
ALTER TABLE match_player_stats ADD COLUMN IF NOT EXISTS most_killed      JSONB DEFAULT '{}';
ALTER TABLE match_player_stats ADD COLUMN IF NOT EXISTS death_by         JSONB DEFAULT '{}';
ALTER TABLE match_player_stats ADD COLUMN IF NOT EXISTS most_killed_ids  JSONB DEFAULT '{}';
ALTER TABLE match_player_stats ADD COLUMN IF NOT EXISTS death_by_ids     JSONB DEFAULT '{}';
ALTER TABLE match_player_stats ADD COLUMN IF NOT EXISTS vehicles_destroyed INT DEFAULT 0;

-- ── Vista: stats acumulados ───────────────────────────────────
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
    guild_id                 BIGINT PRIMARY KEY,
    stats_channel_id         BIGINT,
    snapshot_channel_id      BIGINT,
    challenge_channel_id     BIGINT,
    vinculados_channel_id    BIGINT,
    vinculados_message_id    BIGINT,
    eventos_channel_id       BIGINT,
    server_status_channel_id BIGINT,
    server_status_message_id BIGINT,
    log_channel_id           BIGINT,
    admin_role_id            BIGINT,
    mod_role_id              BIGINT,
    seed_role_id             BIGINT,
    seed_channel_id          BIGINT,
    seed_threshold           INT DEFAULT 40,
    seed_last_notified       TIMESTAMPTZ,
    snapshot_last_fired      DATE,
    language                 VARCHAR(5) DEFAULT 'es',
    created_at               TIMESTAMPTZ DEFAULT NOW(),
    updated_at               TIMESTAMPTZ DEFAULT NOW()
);

-- Migraciones guild_config
ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS server_status_channel_id BIGINT;
ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS server_status_message_id BIGINT;
ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS seed_role_id             BIGINT;
ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS seed_channel_id          BIGINT;
ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS seed_threshold           INT DEFAULT 40;
ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS seed_last_notified       TIMESTAMPTZ;
ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS snapshot_last_fired      DATE;

-- ── Eventos detectados en vivo ────────────────────────────────
CREATE TABLE IF NOT EXISTS detected_events (
    id          SERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    event_type  VARCHAR(30) NOT NULL,
    message     TEXT NOT NULL,
    notified    BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_detected_events_pending ON detected_events (guild_id, notified);

-- ── Sistema de desafíos ───────────────────────────────────────
DROP TABLE IF EXISTS challenge_progress CASCADE;
DROP TABLE IF EXISTS challenge_metric_progress CASCADE;
DROP TABLE IF EXISTS challenge_metrics CASCADE;
DROP TABLE IF EXISTS challenges CASCADE;

CREATE TABLE challenges (
    id                          SERIAL PRIMARY KEY,
    guild_id                    BIGINT NOT NULL,
    name                        VARCHAR(150) NOT NULL,
    description                 VARCHAR(500),
    period                      VARCHAR(15) NOT NULL,
    start_date                  TIMESTAMPTZ,
    end_date                    TIMESTAMPTZ,
    match_id                    VARCHAR(64),
    anchor_map_start            BIGINT,
    map_name                    VARCHAR(150),
    map_start                   BIGINT,
    active                      BOOLEAN DEFAULT TRUE,
    notify_ingame               BOOLEAN DEFAULT FALSE,
    pending_close_notification  BOOLEAN DEFAULT FALSE,
    closed_at                   TIMESTAMPTZ,
    created_by                  BIGINT,
    premio_vip_dias             INT DEFAULT 0,
    created_at                  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_challenges_active ON challenges (guild_id, active);

CREATE TABLE challenge_metrics (
    id              SERIAL PRIMARY KEY,
    challenge_id    INT REFERENCES challenges(id) ON DELETE CASCADE,
    metric          VARCHAR(30) NOT NULL,
    target          NUMERIC NOT NULL,
    param           VARCHAR(150)
);

CREATE INDEX idx_metrics_challenge ON challenge_metrics (challenge_id);

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

-- Migración: premio_vip_dias en challenges
ALTER TABLE challenges ADD COLUMN IF NOT EXISTS premio_vip_dias INT DEFAULT 0;

-- ── Mensajes automáticos in-game ──────────────────────────────
CREATE TABLE IF NOT EXISTS auto_messages (
    guild_id        BIGINT PRIMARY KEY,
    activo          BOOLEAN DEFAULT TRUE,
    intervalo_min   INT DEFAULT 15,
    mensajes        JSONB DEFAULT '[]'::jsonb,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);