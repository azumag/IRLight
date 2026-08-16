.PHONY: up down restart logs ps build test smoke publish preview mute unmute

COMPOSE := docker compose -f docker-compose.poc.yml

up:
	$(COMPOSE) up -d --build

build:
	$(COMPOSE) build

down:
	$(COMPOSE) down --remove-orphans

restart:
	$(COMPOSE) restart continuity

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

test:
	python3 -m unittest discover -s poc/continuity -p 'test_*.py' -v
	python3 -m py_compile poc/continuity/app.py poc/continuity/state.py poc/scripts/set-audio.py poc/scripts/wait-state.py
	bash -n poc/scripts/publish-test.sh poc/scripts/watch-output.sh poc/scripts/smoke-compose.sh

smoke:
	./poc/scripts/smoke-compose.sh

publish:
	./poc/scripts/publish-test.sh

preview:
	./poc/scripts/watch-output.sh

mute:
	./poc/scripts/set-audio.py MUTED

unmute:
	./poc/scripts/set-audio.py LIVE
