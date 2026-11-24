#!/bin/sh
# Анонимная статистика по дням (время сервера, UTC). Запуск: Q05_STATS_DIR=/opt/q05-stats sh gen-stats.sh
#   открытий — зашли на страницу (раз за вкладку)    нажатий — «Посчитать»
#   уник. — первое нажатие на устройстве за день      новых — первое нажатие на устройстве вообще
#   по ссылке — открыли по ссылке с номером           ссылок — нажали «Скопировать ссылку»
d=${Q05_STATS_DIR:-/opt/q05-tracker}/logs
ev=$d/events.log
[ -f "$ev" ] || { echo "пока нет событий"; exit 0; }

awk '
{ day = substr($1, 1, 10) }
$2 == "/open"  { open[day]++;  if ($6 == 1) link[day]++ }
$2 == "/hit"   { hit[day]++;   if ($4 == 1) uniq[day]++; if ($3 == 1) nw[day]++; if ($5 != "-") cat[$5]++ }
$2 == "/share" { share[day]++ }
{ days[day] = 1 }
END {
  printf "%-11s %8s %8s %6s %6s %10s %7s\n", "день", "открытий", "нажатий", "уник.", "новых", "по ссылке", "ссылок"
  fflush()
  for (k in days) {
    printf "%-11s %8d %8d %6d %6d %10d %7d\n", k, open[k], hit[k], uniq[k], nw[k], link[k], share[k] | "sort"
    to += open[k]; th += hit[k]; tu += uniq[k]; tn += nw[k]; tl += link[k]; ts += share[k]
  }
  close("sort")
  printf "%-11s %8d %8d %6d %6d %10d %7d\n", "всего", to, th, tu, tn, tl, ts
  if (to > 0) printf "\nконверсия открытие → нажатие: %.0f%%\n", 100 * th / to
  print "\nпо категориям (нажатий):"
  for (c in cat) printf "  %s: %d\n", c, cat[c]
}' "$ev"

[ -f "$d/clicks-old.log" ] && echo "\nДо подробной статистики (без разбивки): $(wc -l < "$d/clicks-old.log") нажатий"
