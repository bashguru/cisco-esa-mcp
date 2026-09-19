.PHONY: help build up down logs ingest stats tunnel add-user list-users psql fmt test

help:
	@echo "Targets:"
	@echo "  make up          - build + start db and mcp (local mode)"
	@echo "  make tunnel      - build + start with the Cloudflare Tunnel profile"
	@echo "  make down        - stop everything"
	@echo "  make ingest      - ingest the folder mounted at /data/input"
	@echo "  make stats       - print corpus counts"
	@echo "  make logs        - follow mcp logs"
	@echo "  make add-user NAME=Alice [CLIENT_ID=xxx.access | CREATE=1]"
	@echo "  make list-users  - show configured remote users"
	@echo "  make psql        - open a psql shell into the database"
	@echo "  make test        - run the unit tests"

build:
	docker compose build

up:
	docker compose up -d --build

tunnel:
	docker compose --profile tunnel up -d --build

down:
	docker compose --profile tunnel down

logs:
	docker compose logs -f mcp

ingest:
	docker compose run --rm mcp python scripts/ingest.py

stats:
	docker compose run --rm mcp python -c "from cisco_mcp.db import corpus_stats; print(corpus_stats())"

list-users:
	docker compose run --rm mcp python scripts/list_users.py

add-user:
	docker compose run --rm \
		$(if $(CREATE),-e CF_API_TOKEN -e CF_ACCOUNT_ID,) \
		mcp python scripts/add_user.py --name "$(NAME)" \
		$(if $(CLIENT_ID),--client-id $(CLIENT_ID),) $(if $(CREATE),--create,)

psql:
	docker compose exec db psql -U $${POSTGRES_USER:-cisco} -d $${POSTGRES_DB:-ciscodocs}

fmt:
	python -m pyflakes src || true

test:
	python -m pytest -q
