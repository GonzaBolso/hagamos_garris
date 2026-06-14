-- ── Usuarios vinculados Discord <-> Steam ────────────────────
CREATE TABLE IF NOT EXISTS linked_players (
    discord_id      BIGINT PRIMARY KEY,
    steam_id        VARCHAR(20) NOT NULL UNIQUE,
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
    steam_id        VARCHAR(20) NOT NULL,
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
