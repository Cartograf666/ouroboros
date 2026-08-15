# Antigravity через официальный CLI

Ouroboros подключает Antigravity как отдельный read-only консультативный
исполнитель через официальный `agy` CLI. Это не новый API-провайдер и не
OpenAI-compatible proxy: Google OAuth остаётся внутри Antigravity CLI и системного
keychain.

## Авторизация и проверка

Установка CLI:

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Проверка входа и списка доступных моделей:

```sh
agy models
```

Если CLI ещё не авторизован, Antigravity сам предложит пройти Google OAuth. В
Ouroboros ключ или refresh token для этого не задаётся.

## Как пользоваться в чате

Попросите Ouroboros явно обратиться к Antigravity, например:

> Спроси Antigravity, насколько безопасен этот план миграции, и сравни ответ с
> твоим.

Внутри агент вызывает инструмент `antigravity_ask`. Можно указать `model` из
`agy models` и `effort` (`low`, `medium`, `high`). Каждый вызов запускается в
активном workspace с `--mode plan --sandbox`; файлы, коммиты и настройки не
изменяются.

## Что это не делает

Официальный CLI не предоставляет документированный OpenAI-compatible endpoint,
поэтому Antigravity не появляется как обычная модель в основном селекторе
Ouroboros или в Telegram. Это намеренное ограничение: добавление его туда через
перехват локального API/CSRF было бы неофициальным мостом и могло бы нарушить
условия OAuth-сессии. Полноценные длительные `start/wait/cancel` coding-runs
потребуют отдельного durable adapter; текущая интеграция безопасно закрывает
консультации, review, планы и улучшение промтов.
