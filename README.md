# BOT Stats Hagamos_garris
## Pasos:
- Configurar canales /hlladmin setchannel canal:#comandos canal_snapshots:#snapshots canal_desafios:#desafios canal_vinculados:#cuentas_vinculadas canal_eventos:#general
- Configurar roles /hlladmin setroles [@admin] [@player]
- Ver config con /hlladmin config

## Comandos disponibles para ADMIN
| Comando                                                      | Descripción                                 |
|--------------------------------------------------------------|---------------------------------------------|
| `/hlladmin setroles admin:@TuRolAdmin player:@TuRolPlayer`   | Set de Roles ecistentes de admin y Player   |
| `/hlladmin setchannel #canal`                                | Set de Canal existente para comandos        |
| `/hlladmin config`                                           | Ver las configuraciones de canales, roles   |
| `/hlladmin desafio crear`                                    | Crear  desafio                              |
| `/hlladmin desafio eliminar <id>`                            | Eliminar  desafio                           |
| `/hlladmin snapshot`                                         | manda el resumen del día, ahora mismo       |
| `/hlladmin snapshot periodo:Semana`                          | manda el resumen de la semana, ahora mismo  |
| `/hlladmin snapshot periodo:Mes`                             | manda el resumen del mes, ahora mismo       |


## Comandos disponibles para players
| Comando                           | Descripción                                   |
|-----------------------------------|-----------------------------------------------|
| `/hll help`                       | muestra los comandos                          |
| `/hll registro <steam_id/consola>` | Vincula tu Discord con tu Steam ID            |
| `/hll perfil`                     | Tu perfil en CRCON (sesiones, horas, VIP)     |
| `/hll server`                     | Estado del servidor (mapa, jugadores, score)  |
| `/hll online`                     | Jugadores conectados ahora mismo              |
| `/hll vip`                        | Verificá si tenés VIP activo                  |
| `/hll top <categoria>`            | Ranking histórico: Kills, K/D, Partidas, etc. |
| `/stats show`                     | Tus stats acumulados                          |
| `/stats games [cantidad]`         | Tus últimas N partidas                        |
| `/stats weapon`                   | Todas tus armas y kills + Ranking             |
| `/hll desafio listar`             | Listar Desafios disponibles                   |
| `/hll desafio progreso <id>`      | Progreso del desafio <id>                     |

/hlladmin desafio crear nombre:Bazuquero metricas:kills_weapon:$ARMA:10 periodo:Partida actual arma:[autocompletado, tipeá "baz" y elegí BAZOOKA]

/hlladmin desafio crear nombre:Cacería metricas:kills_player:$JUGADOR:5 periodo:Personalizado fecha_fin:01/07/2026 22:00:00 jugador_victima:[autocompletado, tipeá el nombre y elegí]