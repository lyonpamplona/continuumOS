# Contribuindo com o continnumOS

Obrigado por dedicar tempo a melhorar o projeto! Este guia resume o fluxo de contribui&#231;&#227;o.

## Ambiente de desenvolvimento

```bash
# dependencias de desenvolvimento (inclui pytest e alembic)
pip install -r requirements-dev.txt

# subir infraestrutura local
docker compose up -d postgres_db redis

# rodar a aplicacao
uvicorn main:app --reload
```

## Padr&#245;es de c&#243;digo

- **Python 3.11**, estilo consistente com o c&#243;digo existente.
- Mantenha o **isolamento multi-tenant**: toda consulta deve respeitar o tenant do usu&#225;rio autenticado (RLS).
- Logica pura (c&#225;lculos) fica nos *engines* (`ai_engine`, `sales_engine`, `team_engine`), isolada e test&#225;vel.
- N&#227;o introduza segredos no c&#243;digo nem no controle de vers&#227;o.

## Commits e branches

- Use branches descritivas: `feat/...`, `fix/...`, `docs/...`, `chore/...`.
- Prefira mensagens no estilo **Conventional Commits** (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`).
- Abra **Pull Requests** pequenos e focados, com descri&#231;&#227;o do porqu&#234; e do como.

## Testes

```bash
pytest                     # suite completa
RUN_DB_TESTS=1 pytest      # inclui integracao + isolamento RLS
```

- Toda mudan&#231;a de comportamento deve vir acompanhada de teste.
- Mudan&#231;as de schema exigem uma migration Alembic:
  ```bash
  alembic revision --autogenerate -m "descricao"
  ```

## Checklist do PR

- [ ] `pytest` passando localmente.
- [ ] Migration criada (se mudou o schema).
- [ ] Documenta&#231;&#227;o atualizada (README/docs), quando aplic&#225;vel.
- [ ] Sem segredos ou dados sens&#237;veis no diff.

## Nota sobre IA

Parte do desenvolvimento &#233; feita com aux&#237;lio de IA. Contribui&#231;&#245;es assistidas por IA s&#227;o bem-vindas, desde que **revisadas, testadas e compreendidas** por quem as submete.
