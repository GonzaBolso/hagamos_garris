# BOT Stats Hagamos_garris
## Pasos:
- Configurar canales /hlladmin setchannel [Canal de Comandos] [Canal de Snapshots]
- Configurar roles /hlladmin setroles [@admin] [@player]
- Ver config con /hlladmin config

## Comandos disponibles para ADMIN
| Comando                                                    | Descripción                                 |
|------------------------------------------------------------|---------------------------------------------|
| `/hlladmin setroles admin:@TuRolAdmin player:@TuRolPlayer` | Set de Roles ecistentes de admin y Player   |
| `/hlladmin setchannel #tu-canal`                           | Set de Canal existente para comandos        |
| `/hlladmin config`                                         | Ver las configuraciones de canales, roles   |
| `/hlladmin desafio crear`                                  | Crear  desafio                              |
| `/hlladmin desafio eliminar <id>`                          | Eliminar  desafio                           |
| `/hlladmin snapshot`                                       | manda el resumen del día, ahora mismo       |
| `/hlladmin snapshot periodo:Semana`                        | manda el resumen de la semana, ahora mismo  |
| `/hlladmin snapshot periodo:Mes`                           | manda el resumen del mes, ahora mismo       |


## Comandos disponibles para players
| Comando                      | Descripción                                   |
|------------------------------|-----------------------------------------------|
| `/hll help`                  | muestra los comandos                          |
| `/hll registro <steam_id>`   | Vincula tu Discord con tu Steam ID            |
| `/hll perfil`                | Tu perfil en CRCON (sesiones, horas, VIP)     |
| `/hll server`                | Estado del servidor (mapa, jugadores, score)  |
| `/hll online`                | Jugadores conectados ahora mismo              |
| `/hll vip`                   | Verificá si tenés VIP activo                  |
| `/hll top <categoria>`       | Ranking histórico: Kills, K/D, Partidas, etc. |
| `/stats show`                | Tus stats acumulados                          |
| `/stats games [cantidad]`    | Tus últimas N partidas                        |
| `/hll desafio listar`        | Listar Desafio                                |
| `/hll desafio progreso <id>` | Progreso del desafio                          |

