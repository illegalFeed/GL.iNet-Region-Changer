#!/bin/sh
# SPDX-License-Identifier: MIT
# Region-change method: Aaron Viehl (Admon), 2024; see LICENSE and NOTICE.md.
# Client workflow and stricter checks: illegalFeed, 2026.
# POSIX sh; intended runtime: GL.iNet stock firmware / BusyBox ash.

VERSION=1.0.0-rc1
COUNTRY=
CHECK_ONLY=0
WORK=
BACKUP=
LOCK_HELD=0
WRITE_STARTED=0
WRITE_VERIFIED=0

say() { printf '%s\n' "$*"; }
die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }
cleanup() {
    if [ "$WRITE_STARTED" = 1 ] && [ "$WRITE_VERIFIED" != 1 ]; then
        printf '\nЗапись не подтверждена. Не сбрасывайте настройки и не перезагружайте роутер.\nКопия Factory: %s\nПередайте продавцу сообщение об ошибке.\n' "$BACKUP" >&2
    fi
    [ -z "$WORK" ] || rm -rf "$WORK"
    [ "$LOCK_HELD" != 1 ] || rmdir /tmp/beryl-region.lock 2>/dev/null
    :
}
help() {
    say "Beryl AX — помощник смены региона $VERSION"
    say 'Запуск: sh beryl-region.sh [--check] [--country=RU|US|DE|GB]'
    say '--check: только проверка совместимости и поиск CN, без записи.'
    say 'Поддерживается только GL-MT3000 со штатной прошивкой GL.iNet.'
}
confirm() {
    printf '%s\nВведите %s и нажмите Enter: ' "$1" "$2"
    IFS= read -r ANSWER || return 1
    [ "$ANSWER" = "$2" ]
}
dependencies() {
    for CMD in id cat awk dd od strings grep wc cmp cp mkdir mktemp date chmod sync rm rmdir; do
        command -v "$CMD" >/dev/null 2>&1 || die "Нет команды $CMD. Обратитесь к продавцу."
    done
}
hardware() {
    [ "$(id -u)" = 0 ] || die 'Запустите через SSH с именем root.'
    [ -r /tmp/sysinfo/board_name ] || die 'Не удалось определить модель роутера.'
    BOARD=$(cat /tmp/sysinfo/board_name) || die 'Не удалось прочитать модель.'
    case "$BOARD" in glinet,gl-mt3000|glinet,mt3000-snand|gl-mt3000) ;; *) die "Модель $BOARD не поддерживается. Нужен GL-MT3000." ;; esac
    [ -r /etc/glversion ] || die 'Штатная прошивка GL.iNet не определена.'
    FIRMWARE=$(cat /etc/glversion) || die 'Не удалось прочитать версию прошивки.'
    [ -r /proc/mtd ] || die 'Таблица MTD недоступна.'
    # Accept only a single exact Factory label; never guess a partition number.
    MTD_RECORD=$(awk 'tolower($4)=="\"factory\"" {print $1, $2}' /proc/mtd) || die 'Не удалось прочитать MTD.'
    # Intentional splitting: awk emits only a device token and a size token.
    # shellcheck disable=SC2086
    set -- $MTD_RECORD
    [ "$#" = 2 ] || die 'Раздел Factory не найден или найдено несколько разделов.'
    case "$1" in mtd[0-9]*:) ;; *) die 'Неизвестный формат MTD.' ;; esac
    MTD_INDEX=${1#mtd}; MTD_INDEX=${MTD_INDEX%:}
    case "$MTD_INDEX" in ''|*[!0-9]*) die 'Некорректный номер MTD.' ;; esac
    case "$2" in ''|*[!0-9a-fA-F]*) die 'Некорректный размер MTD.' ;; esac
    EXPECTED_SIZE=$((0x$2))
    if [ "$EXPECTED_SIZE" -lt 65536 ] || [ "$EXPECTED_SIZE" -gt 4194304 ]; then
        die 'Неожиданный размер Factory.'
    fi
    DEVICE=/dev/mtdblock$MTD_INDEX
    if ! [ -b "$DEVICE" ] || ! [ -r "$DEVICE" ]; then
        die 'Раздел Factory недоступен.'
    fi
    say "Модель: GL-MT3000 (Beryl AX). Прошивка: $FIRMWARE"
}
snapshot() {
    dd if="$DEVICE" of="$1" bs=65536 2>"$WORK/dd.log" || die 'Не удалось прочитать Factory.'
    SIZE=$(wc -c < "$1") || die 'Не удалось проверить размер копии.'
    [ "$SIZE" -eq "$EXPECTED_SIZE" ] || die 'Factory прочитан не полностью.'
}
plan_image() {
    # Both original signatures are considered together. Numeric byte parsing
    # cannot accidentally match across half-byte boundaries of a hex string.
    strings "$1" > "$WORK/strings" || die 'Не удалось проверить содержимое Factory.'
    if ! grep -q firsttest "$WORK/strings" || ! grep -q secondtest "$WORK/strings"; then
        die 'Структура Factory не распознана.'
    fi
    od -An -v -tu1 "$1" > "$WORK/bytes" || die 'Не удалось прочитать байты Factory.'
    awk '{for(i=1;i<=NF;i++){c=$i+0; if((a==0 || a==255) && b==67 && c==78 && n>=2) print n-1; a=b; b=c; n++}}' "$WORK/bytes" > "$WORK/offsets" || die 'Ошибка поиска региона.'
    COUNT=$(wc -l < "$WORK/offsets") || die 'Ошибка подсчёта совпадений.'
    case "$COUNT" in
        0) die 'Сигнатура CN не найдена. Возможно, регион уже изменён или эта прошивка несовместима. Ничего не записано.' ;;
        1) OFFSET=$(cat "$WORK/offsets") ;;
        *) die 'Найдено несколько сигнатур CN. Автоматическая запись остановлена; обратитесь к продавцу.' ;;
    esac
    if [ "$OFFSET" -le 0 ] || [ "$OFFSET" -ge $((EXPECTED_SIZE - 1)) ]; then
        die 'Некорректное положение кода региона.'
    fi
}
prepare_image() {
    cp "$1" "$WORK/expected.img" || die 'Не удалось подготовить проверочный образ.'
    printf '%s' "$COUNTRY" > "$WORK/country" || die 'Не удалось подготовить код региона.'
    dd if="$WORK/country" of="$WORK/expected.img" bs=1 seek="$OFFSET" count=2 conv=notrunc 2>"$WORK/dd.log" || die 'Не удалось подготовить изменение.'
    [ "$(wc -c < "$WORK/expected.img")" -eq "$EXPECTED_SIZE" ] || die 'Размер проверочного образа изменился.'
}
backup_image() {
    [ ! -L /root/beryl-region-backups ] || die 'Каталог копий не должен быть символической ссылкой.'
    mkdir -p /root/beryl-region-backups || die 'Не удалось создать каталог копий.'
    chmod 700 /root/beryl-region-backups || die 'Не удалось защитить каталог копий.'
    BACKUP_DIR=/root/beryl-region-backups/$(date +%Y%m%d_%H%M%S)_$$
    mkdir "$BACKUP_DIR" || die 'Не удалось создать отдельный каталог копии.'
    BACKUP=$BACKUP_DIR/factory-original.img
    cp "$WORK/original.img" "$BACKUP" || die 'Не удалось сохранить копию Factory.'
    cmp -s "$WORK/original.img" "$BACKUP" || die 'Копия Factory не прошла проверку.'
    printf 'Model: GL-MT3000\nFirmware: %s\nTarget: %s\nOffset: %s\nFactory size: %s\n' "$FIRMWARE" "$COUNTRY" "$OFFSET" "$EXPECTED_SIZE" > "$BACKUP_DIR/info.txt" || die 'Не удалось сохранить сведения о копии.'
    sync || die 'Не удалось завершить сохранение копии.'
}
write_region() {
    # Re-read before writing: no change since the validated backup is allowed.
    snapshot "$WORK/before-write.img"
    cmp -s "$WORK/original.img" "$WORK/before-write.img" || die 'Factory изменился во время работы. Запись отменена.'
    WRITE_STARTED=1
    dd if="$WORK/country" of="$DEVICE" bs=1 seek="$OFFSET" count=2 conv=notrunc 2>"$WORK/dd.log" || die 'Команда записи завершилась с ошибкой.'
    sync || die 'Ошибка завершения записи.'
    snapshot "$WORK/after.img"
    cmp -s "$WORK/expected.img" "$WORK/after.img" || die 'Содержимое Factory после записи отличается от ожидаемого.'
    WRITE_VERIFIED=1
}
choose_country() {
    say 'Выберите регион: RU — Россия, US — США, DE — Германия, GB — Великобритания.'
    say 'US — значение по умолчанию из исходного скрипта. Это код региона устройства; скрипт не настраивает радиоканалы и мощность Wi-Fi.'
    if [ -z "$COUNTRY" ]; then
        printf 'Код региона [US]: '
        IFS= read -r COUNTRY || die 'Ввод закрыт. Запустите мастер из интерактивного SSH.'
        COUNTRY=${COUNTRY:-US}
    fi
    case "$COUNTRY" in RU|US|DE|GB) ;; *) die 'Допустимы RU, US, DE, GB (заглавными буквами).' ;; esac
}
main() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --check) CHECK_ONLY=1 ;;
            --country=*) COUNTRY=${1#*=}; case "$COUNTRY" in RU|US|DE|GB) ;; *) die 'Неизвестный код региона.' ;; esac ;;
            --help|-h) help; exit 0 ;;
            *) die "Неизвестный параметр: $1. Для справки: --help" ;;
        esac
        shift
    done
    umask 077
    dependencies
    hardware
    if [ "$CHECK_ONLY" != 1 ]; then
        if ! [ -t 0 ] || ! [ -t 1 ]; then
            die 'Нужен интерактивный SSH. Сначала скачайте файл, затем запустите его через sh.'
        fi
        [ -w "$DEVICE" ] || die 'Factory недоступен для записи.'
    fi
    mkdir /tmp/beryl-region.lock 2>/dev/null || die 'Другой экземпляр уже работает или остался его lock-каталог. Обратитесь к продавцу.'
    LOCK_HELD=1
    trap cleanup 0
    trap 'exit 1' 1 2 15
    WORK=$(mktemp -d /tmp/beryl-region.XXXXXX) || die 'Не удалось создать рабочий каталог.'
    say "Beryl AX — помощник смены региона $VERSION"
    say 'Проверяем Factory. Запись ещё не выполнялась.'
    snapshot "$WORK/original.img"
    plan_image "$WORK/original.img"
    say 'Найдена одна сигнатура CN. Структура Factory прошла проверки.'
    if [ "$CHECK_ONLY" = 1 ]; then
        say 'Проверка завершена. Ничего не изменено. Это проверка структуры, а не гарантия работы на этой прошивке.'
        exit 0
    fi
    choose_country
    prepare_image "$WORK/original.img"
    say ''
    say "Будет изменён заводской код CN → $COUNTRY. Это неофициальное изменение Factory."
    say 'Для применения потребуется сброс: пароль администратора, настройки Wi-Fi, интернета и VPN будут удалены.'
    say 'Сохраните нужные настройки отдельно. Не отключайте питание во время записи.'
    confirm 'Начать подготовку и резервное копирование?' START || { say 'Отменено. Factory не изменён.'; exit 0; }
    backup_image
    ROUTER_IP=${SSH_CONNECTION:+${SSH_CONNECTION% *}}
    ROUTER_IP=${ROUTER_IP##* }
    ROUTER_IP=${ROUTER_IP:-192.168.8.1}
    say ''
    say 'Сначала сохраните заводскую копию на компьютер.'
    say 'Оставьте это окно открытым. Откройте ВТОРОЕ окно терминала на компьютере и выполните:'
    printf 'scp -O root@%s:%s ./beryl-factory-original.img\n' "$ROUTER_IP" "$BACKUP"
    say 'Если этот файл уже есть на компьютере, выберите другое имя. Убедитесь, что копирование завершилось.'
    say 'Альтернатива для Windows: WinSCP, протокол SCP, тот же IP, логин root, пароль администратора.'
    confirm 'Когда файл сохранён на компьютере, вернитесь в это окно. Записать новый регион?' SAVED || { say 'Отменено. Factory не изменён.'; exit 0; }
    write_region
    say "Готово: CN → $COUNTRY. Весь раздел Factory совпадает с ожидаемым образом."
    say 'Теперь нужен сброс. Заводская копия на роутере при сбросе будет удалена; храните копию на компьютере.'
    say 'Можно выполнить сброс позже в панели: SYSTEM → Reset Firmware. Затем настройте роутер заново.'
    if confirm 'Сбросить настройки и перезагрузить сейчас? Для отказа просто нажмите Enter.' RESET; then
        if ! command -v firstboot >/dev/null 2>&1 || ! command -v reboot >/dev/null 2>&1; then
            die 'Команды сброса недоступны. Выполните сброс в панели.'
        fi
        say 'Выполняем сброс. После перезагрузки SSH отключится — это ожидаемо.'
        firstboot -y || die 'Сброс не выполнен. Регион записан; используйте сброс в панели.'
        sync || die 'Сбой sync после сброса. Обратитесь к продавцу.'
        reboot || die 'Перезагрузка не выполнена. Перезагрузите роутер из панели.'
    else
        say 'Сброс отложен. До сброса изменение может не отобразиться в панели.'
    fi
}

main "$@"
