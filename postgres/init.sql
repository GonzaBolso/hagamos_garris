-- ── Usuarios vinculados Discord <-> Steam ────────────────────
CREATE TABLE IF NOT EXISTS linked_players (
    discord_id      BIGINT PRIMARY KEY,
    steam_id        VARCHAR(64) NOT NULL UNIQUE,
    discord_name    VARCHAR(100),
    linked_at       TIMESTAMPTZ DEFAULT NOW()
);

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

-- ── Vista: stats acumulados por jugador (para /hll top) ───────
CREATE OR REPLACE VIEW player_totals AS
SELECT
    steam_id,
    MAX(player_name)                                AS last_name,
    COUNT(DISTINCT match_id)                        AS matches_played,
    SUM(kills)                                      AS total_kills,
    SUM(deaths)                                     AS total_deaths,
    CASE WHEN SUM(deaths) = 0
         THEN SUM(kills)::FLOAT
         ELSE ROUND((SUM(kills)::NUMERIC / SUM(deaths)), 2)
    END                                             AS kd_ratio,
    SUM(combat_score)                               AS total_combat,
    SUM(offense_score)                              AS total_offense,
    SUM(defense_score)                              AS total_defense,
    SUM(support_score)                              AS total_support,
    SUM(time_seconds)                               AS total_time_seconds
FROM match_player_stats
GROUP BY steam_id;

-- ── Configuración del servidor de Discord ─────────────────────
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id            BIGINT PRIMARY KEY,
    stats_channel_id    BIGINT,
    log_channel_id      BIGINT,
    admin_role_id       BIGINT,
    mod_role_id         BIGINT,
    language            VARCHAR(5) DEFAULT 'es',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ── Sistema de desafíos con múltiples métricas (AND) ──────────
-- Si ya existían las tablas viejas, las recreamos desde cero.
DROP TABLE IF EXISTS challenge_progress CASCADE;
DROP TABLE IF EXISTS challenge_metric_progress CASCADE;
DROP TABLE IF EXISTS challenge_metrics CASCADE;
DROP TABLE IF EXISTS challenges CASCADE;

CREATE TABLE challenges (
    id              SERIAL PRIMARY KEY,
    guild_id        BIGINT NOT NULL,
    name            VARCHAR(150) NOT NULL,
    description     VARCHAR(500),
    period          VARCHAR(15) NOT NULL,   -- 'daily' | 'weekly' | 'custom' | 'current_match' | 'next_match'
    start_date      TIMESTAMPTZ,            -- NULL hasta que arranque (next_match)
    end_date        TIMESTAMPTZ,            -- NULL hasta que se sepa (current/next_match)
    match_id        VARCHAR(64),            -- partida asociada, solo para current_match/next_match
    active          BOOLEAN DEFAULT TRUE,
    notify_ingame   BOOLEAN DEFAULT FALSE,
    created_by      BIGINT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_challenges_active ON challenges (guild_id, active);

-- Cada desafío tiene 1 o más métricas; TODAS deben cumplirse (AND)
CREATE TABLE challenge_metrics (
    id              SERIAL PRIMARY KEY,
    challenge_id    INT REFERENCES challenges(id) ON DELETE CASCADE,
    metric          VARCHAR(30) NOT NULL,   -- 'kills','kd_ratio','matches','combat','offense','defense','support'
    target          NUMERIC NOT NULL
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