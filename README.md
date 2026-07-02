# BOT Stats Hagamos_garris
## Pasos:
- Configurar canales /hlladmin setchannel canal:#comandos canal_snapshots:#snapshots canal_desafios:#desafios canal_vinculados:#cuentas_vinculadas canal_eventos:#general
- Configurar roles /hlladmin setroles [@admin] [@player]
- Ver config con /hlladmin config

## Comandos disponibles para ADMIN
| Comando                                                    | Descripción                                |
|------------------------------------------------------------|--------------------------------------------|
| `/hlladmin help`                                           | Muestra todos los comandos                 |
| `/hlladmin setroles admin:@TuRolAdmin player:@TuRolPlayer` | Set de Roles ecistentes de admin y Player  |
| `/hlladmin setchannel #canal`                              | Set de Canal existente para comandos       |
| `/hlladmin config`                                         | Ver las configuraciones de canales, roles  |
| `/hlladmin desafio crear`                                  | Crear  desafio                             |
| `/hlladmin desafio eliminar <id>`                          | Eliminar  desafio                          |
| `/hlladmin desafio metricas`                               | Ver las metricas existentes para desafios  |
| `/hlladmin snapshot`                                       | manda el resumen del día, ahora mismo      |


## Comandos disponibles para players
| Comando                            | Descripción                                   |
|------------------------------------|-----------------------------------------------|
| `/hll help`                        | muestra los comandos                          |
| `/hll registro <steam_id/consola>` | Vincula tu Discord con tu Steam ID            |
| `/hll perfil`                      | Tu perfil en CRCON (sesiones, horas, VIP)     |
| `/hll server`                      | Estado del servidor (mapa, jugadores, score)  |
| `/hll online`                      | Jugadores conectados ahora mismo              |
| `/hll vip`                         | Verificá si tenés VIP activo                  |
| `/hll top <categoria>`             | Ranking histórico: Kills, K/D, Partidas, etc. |
| `/hll weapon`                      | Muestra todas las kills del player con weapon |
| `/stats show`                      | Tus stats acumulados                          |
| `/stats games [cantidad]`          | Tus últimas N partidas                        |
| `/stats weapon`                    | Todas tus armas y kills + Ranking             |
| `/hll desafio listar`              | Listar Desafios disponibles                   |
| `/hll desafio progreso <id>`       | Progreso del desafio <id>                     |

### Ejemplos de comandos /hlladmin:
```sql
/hlladmin setchannel @canal:a @canal_snapshots:b @canal_desafios:c @canal_vinculados:d @canal_eventos:e
(obligatorio) #canal - Canal para los comandos de los players
(Opcional) #canal_snapshots - Canal para los snapshots (Mensajes automáticos)
(Opcional) #canal_desafios - Canal para los desafios
(Opcional) #canal_vinculados - Canal para las cuentas vinculadas con /hll registro <--->
(Opcional) #canal_eventos - Canal para los eventos (Fakeos, ...)
```
### Ejemplos de comandos /hlladmin:
#### Antes de usar el comando se puede usar ```/hlladmin desafio metricas```sql para ver las metricas disponibles.
```sql
/hlladmin desafio crear #nombre:nombre_kills_actual #metricas:kills:10 #periodo:Partida actual
/hlladmin desafio crear #nombre:nombre_kills_combat #metricas:kills:10,combat:400 #periodo:Partida actual
/hlladmin desafio crear #nombre:nombre_kills_combat_defense #metricas:kills:10,combat:400,defense:500 #periodo:Partida actual

/hlladmin desafio crear #nombre:nombre_kills_perzo #metricas:kills:10 #periodo:Personalizado #fecha_inicio:01/07/2026 10:00:00 #fecha_fin:01/07/2026 20:00:00
/hlladmin desafio crear nombre:Bazuquero metricas:kills_weapon:$ARMA:10 periodo:Partida actual arma:[autocompletado, tipeá "baz" y elegí BAZOOKA]
/hlladmin desafio crear nombre:Cacería metricas:kills_player:$JUGADOR:5 periodo:Personalizado fecha_fin:01/07/2026 22:00:00 jugador_victima:[autocompletado, tipeá el nombre y elegí]
```