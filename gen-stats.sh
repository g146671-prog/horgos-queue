#!/bin/sh
# Собирает data/stats.json из logs/events.log для страницы /stats/. Запускается по cron.
# Только sh + awk (на сервере может быть mawk). Время в журнале — UTC.
base=${Q05_STATS_DIR:-/opt/q05-tracker}
ev=$base/logs/events.log
old=0
[ -f "$base/logs/clicks-old.log" ] && old=$(wc -l < "$base/logs/clicks-old.log")
[ -f "$ev" ] || : > /dev/null
tmp=$base/data/stats.json.tmp

{
  printf '{"updated":"%s","old_clicks":%s,' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$old"
  awk '
  { day = substr($1, 1, 10); hr = substr($1, 12, 2) + 0; days[day] = 1 }
  $2 == "/open"  { open[day]++; if ($6 == 1) link[day]++ }
  $2 == "/hit"   { hit[day]++; hours[hr]++; if ($4 == 1) uniq[day]++; if ($3 == 1) nw[day]++; if ($5 != "-") cat[$5]++ }
  $2 == "/share" { share[day]++ }
  END {
    printf "\"days\":["
    first = 1
    for (k in days) {
      if (!first) printf ","
      first = 0
      printf "{\"d\":\"%s\",\"open\":%d,\"hit\":%d,\"uniq\":%d,\"new\":%d,\"link\":%d,\"share\":%d}", k, open[k], hit[k], uniq[k], nw[k], link[k], share[k]
    }
    printf "],\"cats\":{"
    first = 1
    for (c in cat) { if (!first) printf ","; first = 0; printf "\"%s\":%d", c, cat[c] }
    printf "},\"hours\":["
    for (h = 0; h < 24; h++) { if (h) printf ","; printf "%d", hours[h] }
    printf "]}\n"
  }' "$ev"
} > "$tmp" && mv "$tmp" "$base/data/stats.json"
