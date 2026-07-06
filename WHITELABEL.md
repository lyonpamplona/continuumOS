# White-label — Domínio próprio e marca por cliente

O continnumOS CRM permite que cada tenant use sua própria marca (nome, cor, logo) e
seu próprio domínio.

## Branding

Admin do tenant (permissão `settings:manage`):

```
POST /api/branding
{
  "display_name": "Minha Empresa",
  "brand_color": "#0055FF",
  "logo_url": "https://cdn.exemplo.com/logo.png",
  "domain": "crm.minhaempresa.com"
}
```

O branding público é resolvido pelo host da requisição:

```
GET /api/branding
{ "display_name": "Minha Empresa", "brand_color": "#0055FF", "logo_url": "...", "domain": "crm.minhaempresa.com" }
```

## Como o tenant é resolvido pelo host

A aplicação identifica o tenant a partir do `Host` da requisição, em duas formas:

1. **Subdomínio da plataforma:** `slug.SEU-DOMINIO` (ex.: `acme.app.continnumos.com`).
   Requer DNS **wildcard** apontando para a aplicação:
   ```
   *.app.continnumos.com  CNAME  app.continnumos.com
   ```
   Configure `BASE_DOMAIN=app.continnumos.com`.

2. **Domínio próprio do cliente** (white-label completo): o cliente aponta o
   próprio domínio para a aplicação e você registra esse domínio no tenant
   (campo `domain`).

## Passo a passo — domínio próprio do cliente

1. O cliente cria um registro DNS:
   ```
   crm.minhaempresa.com  CNAME  app.continnumos.com
   ```
2. Você registra o domínio no tenant via `POST /api/branding` (campo `domain`).
3. Emita um certificado TLS para o domínio (ex.: Let's Encrypt / proxy reverso
   com emissão automática — Caddy, Traefik, Nginx + certbot).
4. Pronto: acessos a `crm.minhaempresa.com` resolvem para o tenant correto
   (`get_tenant_id_by_host`), com a marca do cliente.

## TLS / proxy reverso (recomendação)

- Use um proxy com emissão automática de certificados para domínios dinâmicos
  (on-demand TLS), já que os domínios dos clientes variam.
- Garanta que o header `Host` chegue à aplicação sem reescrita.

## Observações

- O campo `domain` deve ser único entre tenants.
- Sem domínio próprio, o cliente continua acessível via subdomínio da plataforma.
