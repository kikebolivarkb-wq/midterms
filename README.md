# Radar Electoral 2026

Tu propio agregador independiente de encuestas para las elecciones al Congreso de EE. UU. del 3 de noviembre de 2026. Se actualiza solo cada 3 horas, gratis, sin servidor y sin claves de API.

## Fuentes (todas citadas y enlazadas en el panel, sección "Fuentes y verificación")

| Tipo | Fuente | Cómo entra |
|---|---|---|
| Encuestas | VoteHub Polling API (CC BY 4.0) | automática |
| Encuestas | Wikipedia, tablas por carrera vía API de MediaWiki (CC BY-SA 4.0) | automática; verifica y completa VoteHub |
| Calidad de encuestadoras | FiveThirtyEight, archivo público de calificaciones (CC BY 4.0) | automática, con copia local |
| Calidad de encuestadoras | Silver Bulletin, calificaciones enero 2026 | opcional: descarga el XLSX y guárdalo como `config/external/silver_bulletin_pollster_stats.xlsx` |
| Acreditación | AAPOR Transparency Initiative y Roper Center | incluida en los datos de 538 |
| Promedios de control | RealClearPolitics, 270toWin, Decision Desk HQ, FiftyPlusOne, Silver Bulletin, Race to the WH | automática vía Wikipedia; no se suman al promedio |
| Pronosticadores | Cook, Sabato, Inside Elections, The Economist, DDHQ, Fox News, FiftyPlusOne, Split Ticket, Silver Bulletin, RCP, Race to the WH | automática vía Wikipedia; 50 % de la base estructural |
| Mercados | Polymarket, Kalshi, PredictIt | automática; 15 % de la probabilidad final |
| Dinero de campaña | OpenFEC | automática; opcional: clave gratis en api.data.gov guardada como secreto `FEC_API_KEY` |
| Medios | GDELT y Google News | automática; máximo 1 punto de efecto |
| Resultados 2024 | Resultados certificados (referencia: MIT Election Data and Science Lab) | fijos en `config/senate_races.json` |

## Qué hace

1. Descarga las encuestas de dos compilaciones independientes (VoteHub y Wikipedia), las une y marca como verificadas las que aparecen en ambas.
2. Pondera cada encuesta por la precisión histórica medida de la encuestadora (538 o Silver Bulletin), su acreditación AAPOR/Roper, el tamaño de la muestra, la fecha, el tipo de votante y si la pagó un partido.
3. Mide y corrige el sesgo habitual de cada encuestadora ("efecto casa") comparándola con el consenso. Así ninguna firma, de ningún lado, arrastra el promedio.
4. Mezcla ese promedio con una base estructural: 50 % cómo votó cada estado en 2024 más el ambiente nacional, 50 % el consenso de 11 pronosticadores.
5. Compara con los promedios de 6 agregadores y avisa si hay desviaciones grandes. Añade dos capas con peso limitado: tres mercados de predicción y el tono de la cobertura en medios, incluida prensa local (GDELT), más titulares recientes (Google News).
6. Simula 20.000 elecciones con error nacional, regional y por carrera, con colas gruesas, y calcula quién controla el Senado y la Cámara.

## Puesta en marcha (unos 10 minutos, una sola vez)

1. Crea una cuenta en github.com si no tienes.
2. Crea un repositorio nuevo y **público** (por ejemplo `radar-electoral`). Las cuentas gratuitas solo publican páginas desde repositorios públicos.
3. Sube el contenido de esta carpeta: "Add file → Upload files", arrastra todo y pulsa "Commit changes".
4. **Comprueba que se subió la carpeta oculta `.github`.** En macOS y Windows las carpetas que empiezan con punto suelen quedar ocultas y no se suben. En el repositorio debe verse `.github/workflows/update.yml`. Si no está: "Add file → Create new file", escribe como nombre `.github/workflows/update.yml`, pega el contenido de ese archivo y guarda.
5. Settings → Actions → General → Workflow permissions → "Read and write permissions" → Save.
6. Settings → Pages → Source: **GitHub Actions** (no "Deploy from a branch").
7. Pestaña Actions → si aparece un botón para habilitar los flujos de trabajo, púlsalo → "Update" → "Run workflow". Tarda entre 5 y 10 minutos.
8. Cuando termine con marca verde, tu panel queda en `https://TU-USUARIO.github.io/radar-electoral/`. Desde ahí se actualiza solo cada 3 horas.

## Cómo comprobar que se actualiza solo

- Al día siguiente, en la pestaña Actions deberías ver varias ejecuciones de "Update" que no lanzaste tú (dicen "Scheduled").
- En el panel, arriba a la derecha, la hora de actualización debe ser de las últimas 3 horas y el punto verde debe decir "Datos en vivo".
- En la sección "Fuentes y verificación", cada fuente muestra si respondió en la última actualización.

## Qué pasa si algo falla

El sistema está hecho para no romperse ni quedarse callado:

- **Si una fuente se cae** (VoteHub, Wikipedia, un mercado…), usa la última descarga buena de esa fuente y lo avisa en "Calidad de datos". Las demás fuentes siguen funcionando.
- **Si la actualización entera falla o produce datos inválidos**, un autocontrol (`scripts/selftest.py`) lo detecta, la página se vuelve a publicar con los últimos datos buenos y la ejecución queda marcada en rojo. GitHub te envía un correo cuando una ejecución programada falla.
- **Si pasan más de 8 horas sin actualizarse**, el propio panel muestra un aviso rojo arriba indicando que revises la pestaña Actions.
- GitHub puede retrasar las ejecuciones programadas unos minutos en horas de mucha demanda; es normal.
- GitHub pausa los flujos programados de repositorios sin actividad durante 60 días. Aquí no ocurre, porque el propio robot guarda datos en cada ejecución, y en cualquier caso la elección es antes.

## Probar en tu computadora

```
pip install -r requirements.txt
python scripts/update.py            # datos en vivo
python scripts/update.py --offline  # solo datos semilla
cd docs && python -m http.server 8000
```
Abre http://localhost:8000 (abrir el HTML con doble clic no funciona porque el navegador bloquea la lectura de archivos locales).

Para comprobar los datos antes de publicar: `python scripts/selftest.py`.

## Personalizar

Todo se ajusta en `config/` sin tocar código:

- `config.json`: pesos de cada capa (modelo, mercados, medios), vida media de las encuestas, incertidumbre, supuestos de la Cámara.
- `pollsters.json`: calidad de cada encuestadora (A, B, C o excluida).
- `senate_races.json`: los 35 escaños, candidatos y resultado de 2024 por estado. Si una encuesta trae un nombre que el sistema no reconoce, aparece en "Calidad de datos" al final del panel; añádelo aquí.
- `manual_polls.json`: tus propias encuestas o cualquiera que no esté en VoteHub.
- `seed_data.json`: respaldo que se usa solo si VoteHub no responde.

## Límites honestos

- Es un modelo, no una bola de cristal: una probabilidad de 70 % significa que el otro lado gana 3 de cada 10 veces.
- La Cámara se estima desde el voto genérico porque casi ningún distrito tiene encuestas. Los parámetros de redistribución (`tipping_point_generic_margin`, `seats_per_point`) vienen de análisis públicos de junio de 2026 y conviene revisarlos.
- El tono en medios mide cómo se habla de un candidato, no cuánta gente lo votará. Por eso su efecto está limitado a 1 punto.
- Los datos de VoteHub tienen licencia CC BY 4.0: mantén la mención a VoteHub si compartes el panel.
