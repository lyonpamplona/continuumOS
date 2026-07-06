"""Importa VCF e apoia organização de contatos herdados por prefixos.

O script preserva o fluxo antigo de importação, mas também possui modos de
varredura e classificação por prefixos no nome do contato. Isso permite
encontrar padrões como "RO Cliente", ".ro Cliente", "Flavia - Cliente" ou
"Kim [ Cliente ]" e vincular esses contatos a listas operacionais sem alterar
o nome original do cliente. A regra de prefixos é compartilhada com a
ferramenta "Listas por Prefixos" do painel admin.
"""

import argparse
import asyncio
import os
import quopri
import re
import unicodedata
from collections import Counter, defaultdict

import asyncpg

VCF_FILE = "contacts (2).vcf"

PREFIX_ALIASES = {
    "ro": "Rosana",
    "rosana": "Rosana",
    "fla": "Flavia",
    "flavia": "Flavia",
    "flavaia": "Flavia",
    "kim": "Kimberly",
    "kimberly": "Kimberly",
    "denise": "Denise",
    "thalita": "Thalita",
    "sandro": "Sandro",
    "ronaldo": "Ronaldo",
    "rodrigo": "Rodrigo",
    "dayana": "Dayana",
}

LIST_COLORS = {
    "Rosana": "#64b5f6",
    "Flavia": "#64b5f6",
    "Kimberly": "#ba68c8",
    "Denise": "#ffd54f",
    "Thalita": "#f15c6d",
    "Sandro": "#00a884",
    "Ronaldo": "#ff6600",
    "Rodrigo": "#4dd0e1",
    "Dayana": "#81c784",
}


def clean_phone(phone_raw):
    """Normaliza um telefone para o JID esperado pela Evolution API."""
    nums = re.sub(r"\D", "", phone_raw or "")
    if len(nums) <= 11:
        nums = "55" + nums
    return f"{nums}@s.whatsapp.net"


def decode_vcard_text(text):
    """Decodifica textos quoted-printable comuns em arquivos vCard."""
    try:
        if "=" in text:
            return quopri.decodestring(text.encode("utf-8")).decode("utf-8")
    except Exception:
        pass
    return text


def normalize_token(value):
    """Remove acentos e pontuação lateral para comparar prefixos."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", value.lower())


def extract_leading_tokens(name, max_tokens=3):
    """Extrai as primeiras palavras úteis de um nome de contato."""
    if not name:
        return []
    cleaned = decode_vcard_text(name)
    cleaned = re.sub(r"^[\s\.\-_/\\|:;,#@]+", "", cleaned)
    cleaned = cleaned.replace("[", " ").replace("]", " ")
    parts = re.findall(r"[A-Za-zÀ-ÿ0-9]+", cleaned)
    tokens = []
    for part in parts[:max_tokens]:
        token = normalize_token(part)
        if len(token) >= 2:
            tokens.append(token)
    return tokens


def detect_known_prefix(name, approved_prefixes=None):
    """Identifica prefixo conhecido/manual somente no início do nome."""
    tokens = extract_leading_tokens(name)
    approved = {normalize_token(item) for item in approved_prefixes or [] if item}
    if not tokens:
        return None, None
    token = tokens[0]
    if approved and token in approved:
        return token, PREFIX_ALIASES.get(token, token.title())
    if not approved and token in PREFIX_ALIASES:
        return token, PREFIX_ALIASES[token]
    return None, None


def parse_vcf_contacts(path):
    """Lê contatos de um VCF e retorna nome/telefone normalizado."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Arquivo {path} não encontrado.")
    with open(path, "r", encoding="utf-8") as file:
        content = file.read()
    cards = re.findall(r"BEGIN:VCARD(.*?)END:VCARD", content, re.DOTALL)
    contacts = []
    for card in cards:
        name_match = re.search(r"FN(?:;.*?)?:(.*)", card)
        tel_match = re.search(r"TEL(?:;.*?)?:(.*)", card)
        if not name_match or not tel_match:
            continue
        contacts.append(
            {
                "name": decode_vcard_text(name_match.group(1).strip()),
                "phone": clean_phone(tel_match.group(1).strip()),
            }
        )
    return contacts


def build_arg_parser():
    """Cria a interface de linha de comando para importação e listas."""
    parser = argparse.ArgumentParser(description="Importa VCF e organiza contatos por prefixos.")
    parser.add_argument(
        "mode",
        nargs="?",
        default="import",
        choices=["import", "scan-prefixes", "apply-prefix-lists"],
        help="Modo de execução. O padrão mantém a importação antiga.",
    )
    parser.add_argument("--file", default=VCF_FILE, help="Arquivo VCF usado no modo import ou na origem vcf.")
    parser.add_argument("--source", choices=["db", "vcf"], default="db", help="Origem para scan-prefixes.")
    parser.add_argument("--min-count", type=int, default=2, help="Quantidade mínima para mostrar prefixos no relatório.")
    parser.add_argument("--prefixes", default="", help="Prefixos aprovados separados por vírgula. Ex: ro,flavia,kim")
    parser.add_argument("--dry-run", action="store_true", help="Mostra o que seria feito sem gravar no banco.")
    parser.add_argument("--limit-examples", type=int, default=5, help="Quantidade de exemplos exibidos por prefixo/lista.")
    return parser


async def connect_db():
    """Abre conexão PostgreSQL usando `DATABASE_URL` do ambiente."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL não encontrada. Execute este script por dentro do Docker.")
    return await asyncpg.connect(db_url)


async def ensure_contact_list_schema(conn):
    """Garante tabelas de listas para uso independente do startup do app."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_lists (
            id SERIAL PRIMARY KEY,
            name TEXT,
            color TEXT DEFAULT '#00a884',
            owner_name TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name, owner_name)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_list_members (
            list_id INTEGER REFERENCES contact_lists(id) ON DELETE CASCADE,
            phone TEXT REFERENCES users(phone) ON DELETE CASCADE,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(list_id, phone)
        )
        """
    )


async def ensure_contact_list(conn, name, color):
    """Cria ou retorna uma lista global de contatos."""
    row = await conn.fetchrow(
        """
        INSERT INTO contact_lists (name, color, owner_name)
        VALUES ($1, $2, '')
        ON CONFLICT (name, owner_name) DO UPDATE SET color = EXCLUDED.color
        RETURNING id
        """,
        name,
        color,
    )
    return row["id"]


async def fetch_db_contacts(conn):
    """Busca contatos existentes no CRM para saneamento."""
    rows = await conn.fetch("SELECT phone, name, assigned_to, stage FROM users ORDER BY name")
    return [dict(row) for row in rows]


async def import_vcf(args):
    """Executa a importação completa usando o arquivo VCF informado."""
    if not os.path.exists(args.file):
        print(f"Erro: Arquivo {args.file} não encontrado na raiz.")
        return

    print("Conectando ao banco de dados PostgreSQL...")
    conn = await connect_db()

    rows = await conn.fetch("SELECT role, name FROM system_users")
    users_db = {row["role"]: row["name"] for row in rows}

    nome_v1 = users_db.get("vendas_1", "Vendas 1")
    nome_v2 = users_db.get("vendas_2", "Vendas 2")
    nome_fin = users_db.get("financeiro", "Financeiro")

    mapa_vendedoras = {
        "rosana": nome_v2,
        "ro": nome_v2,
        "fla": nome_v2,
        "flavia": nome_v2,
        "flavaia": nome_v2,
        "kim": nome_v1,
        "kimberly": nome_v1,
        "denise": nome_fin,
    }

    print("Lendo e decodificando o arquivo VCF...")
    contacts = parse_vcf_contacts(args.file)

    inseridos = 0
    atualizados = 0

    for contact in contacts:
        raw_name = contact["name"]
        clean_number = contact["phone"]

        assigned_to = "Não Atribuído"
        prefix, _canonical = detect_known_prefix(raw_name)
        if prefix and prefix in mapa_vendedoras:
            assigned_to = mapa_vendedoras[prefix]

        existe = await conn.fetchval("SELECT phone FROM users WHERE phone = $1", clean_number)
        if existe:
            await conn.execute(
                "UPDATE users SET name = $1, assigned_to = $2, stage = 'carteira' WHERE phone = $3",
                raw_name,
                assigned_to,
                clean_number,
            )
            atualizados += 1
        else:
            await conn.execute(
                """
                INSERT INTO users (phone, name, stage, assigned_to, session_id)
                VALUES ($1, $2, 'carteira', $3, 'vendas')
                ON CONFLICT (phone) DO NOTHING
                """,
                clean_number,
                raw_name,
                assigned_to,
            )
            inseridos += 1

    print(f"Importação finalizada: {inseridos} contatos novos e {atualizados} atualizados.")
    await distribute_orphans(conn, nome_v1, nome_v2)
    await conn.close()


async def distribute_orphans(conn, nome_v1, nome_v2):
    """Distribui contatos sem responsável entre as duas filas de venda."""
    print("Iniciando distribuição dos contatos órfãos...")
    vendedoras = [nome_v1, nome_v2]

    contatos_orfaos = await conn.fetch(
        """
        SELECT phone, name FROM users
        WHERE stage = 'carteira' AND (assigned_to = 'Não Atribuído' OR assigned_to IS NULL)
        """
    )

    if not contatos_orfaos:
        print("Nenhum contato órfão encontrado.")
        return

    print(f"Dividindo {len(contatos_orfaos)} contatos entre {vendedoras[0]} e {vendedoras[1]}...")
    for i, record in enumerate(contatos_orfaos):
        vendedora_escolhida = vendedoras[i % 2]
        prefixo = "Rosana" if "rosana" in vendedora_escolhida.lower() else "Kim"
        nome_limpo = record["name"] or "Sem Nome"
        novo_nome = nome_limpo
        if not nome_limpo.startswith("Rosana -") and not nome_limpo.startswith("Kim -"):
            novo_nome = f"{prefixo} - {nome_limpo}"
        await conn.execute(
            "UPDATE users SET assigned_to = $1, name = $2 WHERE phone = $3",
            vendedora_escolhida,
            novo_nome,
            record["phone"],
        )
    print(f"Distribuição concluída: {len(contatos_orfaos)} contatos atualizados.")


def summarize_prefixes(contacts, min_count, limit_examples):
    """Monta relatório de primeiras palavras e prefixos conhecidos."""
    first_tokens = Counter()
    known_tokens = Counter()
    examples = defaultdict(list)

    for contact in contacts:
        name = contact.get("name") or ""
        tokens = extract_leading_tokens(name)
        if not tokens:
            continue
        first = tokens[0]
        first_tokens[first] += 1
        if len(examples[first]) < limit_examples:
            examples[first].append(name)
        prefix, _canonical = detect_known_prefix(name)
        if prefix:
            known_tokens[prefix] += 1

    return first_tokens, known_tokens, examples


async def scan_prefixes(args):
    """Mostra prefixos encontrados sem alterar contatos."""
    if args.source == "vcf":
        contacts = parse_vcf_contacts(args.file)
    else:
        conn = await connect_db()
        contacts = await fetch_db_contacts(conn)
        await conn.close()

    first_tokens, known_tokens, examples = summarize_prefixes(
        contacts,
        args.min_count,
        args.limit_examples,
    )

    print(f"Contatos analisados: {len(contacts)}")
    print("\nPrefixos conhecidos encontrados:")
    if not known_tokens:
        print("  Nenhum prefixo conhecido encontrado.")
    for token, total in known_tokens.most_common():
        canonical = PREFIX_ALIASES.get(token, token.title())
        print(f"  {token} -> {canonical}: {total}")

    print(f"\nPrimeiras palavras com pelo menos {args.min_count} ocorrência(s):")
    for token, total in first_tokens.most_common():
        if total < args.min_count:
            continue
        canonical = PREFIX_ALIASES.get(token, "")
        suffix = f" -> {canonical}" if canonical else ""
        print(f"\n  {token}{suffix}: {total}")
        for example in examples[token]:
            print(f"    - {example}")

    print("\nPara aplicar em listas, use por exemplo:")
    print("  python importar_vcf.py apply-prefix-lists --prefixes ro,flavia,flavaia,kim,kimberly --dry-run")
    print("  python importar_vcf.py apply-prefix-lists --prefixes ro,flavia,flavaia,kim,kimberly")


async def apply_prefix_lists(args):
    """Vincula contatos com prefixos aprovados às listas operacionais."""
    approved = [item.strip() for item in args.prefixes.split(",") if item.strip()]
    if not approved:
        print("Informe os prefixos aprovados com --prefixes. Ex: --prefixes ro,flavia,kimberly")
        return

    conn = await connect_db()
    await ensure_contact_list_schema(conn)
    contacts = await fetch_db_contacts(conn)

    grouped = defaultdict(list)
    for contact in contacts:
        prefix, canonical = detect_known_prefix(contact.get("name"), approved)
        if prefix:
            grouped[canonical].append({**contact, "prefix": prefix})

    if not grouped:
        print("Nenhum contato encontrado com os prefixos informados.")
        await conn.close()
        return

    total_members = 0
    print("Classificação encontrada:")
    for canonical, items in sorted(grouped.items()):
        list_name = f"Herdados {canonical}"
        print(f"\n  {list_name}: {len(items)} contato(s)")
        for item in items[: args.limit_examples]:
            print(f"    - {item.get('name')} | {item.get('phone')}")

        if args.dry_run:
            continue

        list_id = await ensure_contact_list(conn, list_name, LIST_COLORS.get(canonical, "#00a884"))
        for item in items:
            await conn.execute(
                """
                INSERT INTO contact_list_members (list_id, phone)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                list_id,
                item["phone"],
            )
            total_members += 1

    if args.dry_run:
        print("\nDry-run concluído. Nada foi gravado no banco.")
    else:
        print(f"\nListas atualizadas. Vínculos processados: {total_members}.")

    await conn.close()


async def main():
    """Executa o modo selecionado na linha de comando."""
    args = build_arg_parser().parse_args()
    if args.mode == "import":
        await import_vcf(args)
    elif args.mode == "scan-prefixes":
        await scan_prefixes(args)
    elif args.mode == "apply-prefix-lists":
        await apply_prefix_lists(args)


if __name__ == "__main__":
    asyncio.run(main())
