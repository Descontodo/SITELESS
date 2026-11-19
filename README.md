# Siteless

Aplicação Python com interface em português para encontrar estabelecimentos em dados abertos do OpenStreetMap (OSM), classificar presença digital e reunir contatos que os próprios negócios publicaram. Os dados e as buscas ficam separados por conta no SQLite.

## Aplicativo desktop local

O Siteless pode ser empacotado como aplicativo portátil para Windows, macOS e Linux. Ele inicia o servidor FastAPI somente em `127.0.0.1` e abre a interface no navegador padrão. No modo empacotado, o banco SQLite fica na pasta de dados do usuário do sistema operacional; ele não usa espaço em Google Drive nem mantém seu computador ligado para um servidor remoto.

Para gerar o pacote Windows neste repositório, execute `scripts/build_desktop.ps1` no PowerShell. O arquivo `outputs/Siteless-Windows-x64.zip` conterá o executável e as instruções. A geração para macOS e Linux precisa ocorrer em cada sistema; PyInstaller não é um compilador cruzado. O workflow `.github/workflows/desktop-build.yml` prepara pacotes para Windows x64, Linux x64, macOS Intel e Apple Silicon no GitHub Actions, ao receber um commit na branch `main` ou quando iniciado manualmente. Os artefatos ficam disponíveis por 14 dias e não são um lançamento público permanente.

Esta versão desktop é para uso local individual e validação. Os dados ficam no computador em que a busca foi feita. Para comercializar o Siteless como SaaS com usuários sincronizados, quotas e pagamentos ativos, ainda é necessário hospedar o backend e migrar a persistência para um banco de dados em nuvem. O aplicativo desktop não deve ser divulgado como o serviço SaaS final.

Para backups, feche o Siteless e copie o arquivo `leads_sem_site.db` da pasta local de dados para um serviço de arquivos. Google Drive pode armazenar essa cópia e os CSV/XLSX exportados, mas não sincronize o banco ativo entre computadores.

Mais instruções para usuários finais estão em `README-DESKTOP.txt`.

## Instalação local

Requisitos: Python 3.10 ou superior e acesso à internet.

No PowerShell, dentro desta pasta:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

Chromium é usado como fallback para ler páginas públicas que carregam contatos por JavaScript. A busca de estabelecimentos não automatiza o Google Maps nem requer chave paga.

Para iniciar em `http://127.0.0.1:8000`:

```powershell
$env:ADMIN_EMAIL = "seu-email@exemplo.com"
$env:ADMIN_PASSWORD = "uma-senha-forte-com-mais-de-10-caracteres"
$env:COOKIE_SECURE = "false"
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Ao iniciar pela primeira vez, o sistema cria o admin configurado. Se as variáveis não forem definidas, os valores iniciais são `admin@sistema.com` e `Admin@1234`; troque-os antes de usar em qualquer ambiente compartilhado. Em inicializações seguintes, `ADMIN_EMAIL` pode atualizar o e-mail administrativo e `ADMIN_PASSWORD` redefine a senha e revoga sessões anteriores. Se não definir essas variáveis, a credencial já armazenada é mantida.

Outras pessoas podem criar contas pela tela de entrada; novas contas começam no plano Grátis. O painel administrativo fica em `/admin`.

## Como a busca funciona

1. Nominatim resolve o centro e os limites da cidade. O resultado é guardado em cache por 24 horas.
2. Overpass consulta estabelecimentos marcados no OpenStreetMap. O modo profundo (Agency/Admin) encontra bairros nomeados no OSM e inclui os pontos próximos em uma consulta agregada.
3. A aplicação classifica o website e tenta extrair e-mail, telefone/WhatsApp, Instagram, Facebook, TikTok, LinkedIn, YouTube e links de agendamento de tags OSM, HTML público e páginas relacionadas de contato.
4. Quando ainda faltam contatos, `ddgs` faz duas consultas públicas moderadas; há uma pausa aleatória de 2 a 4 segundos entre elas. O crawler HTTP e, se necessário, Chromium tentam ler páginas públicas. Resultados são indicativos e devem ser conferidos.
5. Cada novo lead é gravado imediatamente. O botão **Parar** interrompe a coleta entre as etapas sem apagar registros já salvos.

A biblioteca `duckduckgo-search` passou a ser distribuída com o nome `ddgs`; é esse pacote atual que aparece em `requirements.txt`.

O OSM não fornece cobertura universal, nota/contagem de avaliações ou estado de abertura confiável. Por isso, esses dados ficam vazios/zero e filtros de nota, avaliações ou “abertas agora” não são compatíveis com a fonte. Um número local convertido em link `wa.me` pelo DDI do país é uma sugestão automática, não confirmação de que aquele número possui WhatsApp.

### Fontes públicas e uso comercial

Os estabelecimentos e os mapas vêm de [OpenStreetMap](https://www.openstreetmap.org/copyright), sob a licença ODbL. A interface inclui atribuição. Nominatim e Overpass públicos são serviços compartilhados, com limites e disponibilidade sem garantia; o Nominatim público não deve ser usado para geocodificação sistemática em massa. O código consulta uma cidade por vez, cacheia o resultado e mantém as URLs de serviços configuráveis (`NOMINATIM_URL`, `OVERPASS_URL`, `OSM_USER_AGENT`). Para um SaaS comercial com volume, configure uma instância/fornecedor autorizado e dimensionado, respeitando a ODbL e as políticas do serviço.

Não há raspagem de Google Maps/Places: além de a busca estar baseada em dados abertos, as regras padrão do Google restringem copiar, raspar, armazenar e exportar conteúdo de Maps/Places. Os links de mapa salvos apontam para a entidade OSM.

## Planos e limites

| Plano | Quota por período de 30 dias | Máximo por busca | Recursos |
|---|---:|---:|---|
| Grátis | 15 novos leads | 10 | CSV; contatos completos nos cinco leads mais recentes |
| Starter (US$ 19/mês) | 600 novos leads | 600 | CSV/XLSX, pitch multilíngue e WhatsApp direto |
| Agency (US$ 49/mês) | 3.000 novos leads | 3.000 | Starter, varredura profunda e teste HTTP leve |
| Admin | Ilimitado | 5.000 | Todos os recursos; uso gratuito do proprietário |

O período de quota é uma janela de 30 dias a partir da criação/renovação da conta. Duplicatas atualizam contatos sem consumir quota. Os leads são isolados por `user_id`; a deduplicação é por conta. O CSV do plano Grátis respeita a mesma ocultação de contatos da tabela.

## Stripe: assinaturas do Siteless

```powershell
Copy-Item .env.example .env
```

O Siteless usa Stripe Checkout hospedado. A aplicação cria uma sessão para o usuário autenticado e a confirmação assinada pelo Stripe ativa o plano; não use Payment Links compartilhados. Os preços definidos são Starter por US$ 19/mês e Agency por US$ 49/mês. O Checkout aceita somente Price IDs ativos com valor, moeda USD e recorrência mensal correspondentes ao plano.

No Dashboard, use primeiro o modo de teste e abra **Produtos → Adicionar produto**. Crie ou atualize os produtos **Siteless Starter** e **Siteless Agency** com preços recorrentes mensais de **US$ 19,00** e **US$ 49,00**. A moeda e o valor de um Price existente não podem ser editados: crie novos Prices em USD e substitua os IDs no `.env`. Copie cada ID `price_...` para `STRIPE_PRICE_STARTER_ID` ou `STRIPE_PRICE_AGENCY_ID`. Não crie links de pagamento: o botão do Siteless gera um Checkout exclusivo para cada usuário. Impostos automáticos ficam desligados por enquanto.

Na conta **SiteLess** conectada em live mode, os preços de produção foram criados: Starter `price_1UdYE4GhTbbIxSaVNavwoAC5` e Agency `price_1UdYEBGhTbbIxSaVO2fZsPmV`. Use esses IDs somente junto da chave `sk_live_...`. O modo de teste do Stripe tem IDs separados; não misture Prices de live com chave `sk_test_...`.

Para desenvolvimento, use uma chave de sandbox `sk_test_...` em `STRIPE_SECRET_KEY`. Nunca envie chaves pelo chat, não as inclua no código e não publique o arquivo `.env` (ele está ignorado pelo Git). O arquivo `.env.example` tem somente valores de exemplo.

Para receber eventos localmente, instale e autentique o Stripe CLI e rode em outro terminal:

```powershell
stripe listen --forward-to http://127.0.0.1:8000/api/webhook/stripe
```

Copie o segredo `whsec_...` exibido pelo CLI para `STRIPE_WEBHOOK_SECRET` no `.env` e reinicie o servidor. O endpoint valida o cabeçalho `Stripe-Signature`, registra IDs de eventos para idempotência e sincroniza os planos a partir de `checkout.session.completed`, `invoice.paid`, `invoice.payment_failed` e eventos de assinatura.

Quando o Siteless estiver hospedado em HTTPS, configure `APP_BASE_URL` com o domínio público, troque para uma chave de live mode (`sk_live_...`) e registre no Dashboard o endpoint `https://SEU-DOMINIO/api/webhook/stripe` com os mesmos eventos. Copie o novo segredo `whsec_...` desse endpoint para `STRIPE_WEBHOOK_SECRET`; o segredo local do CLI não serve para produção. Ative no Customer Portal as opções de alteração/cancelamento de assinatura e atualização de pagamento que deseja oferecer aos clientes.

O plano grátis não pede cartão. Depois do Checkout, o cliente pode usar o botão **Gerenciar assinatura** para abrir o portal Stripe. Em `invoice.payment_failed`, o Siteless preserva o acesso durante o período pago e passa para o plano Grátis se o período expirar sem recuperação; o cancelamento confirmado pelo Stripe também retorna a conta ao plano Grátis.

O painel `/admin` permite listar usuários, atribuir os planos `free`, `starter`, `agency` ou `admin`, zerar quota e bloquear/desbloquear contas. Senhas são armazenadas com PBKDF2-HMAC-SHA256 e salt aleatório; sessões usam tokens aleatórios cujo hash é guardado no SQLite e cookie `HttpOnly`/`SameSite=Lax`.

## Banco e exportações

O banco padrão é `leads_sem_site.db`, ao lado de `app.py`. `LEADS_DB_PATH` altera o caminho. Uma migração automática mantém os dados antigos e associa registros sem usuário ao admin inicial. A tabela `leads` e os arquivos CSV/XLSX incluem:

`nome`, `nicho`, `endereco`, `cidade`, `pais`, `telefone`, `whatsapp`, `link_whatsapp_direto`, `email`, `instagram`, `facebook`, `tiktok`, `linkedin`, `youtube`, `link_agendamento`, `nota`, `avaliacoes`, `link_maps`, `motivo_flag`, `criado_em`.

CSV usa UTF-8 com BOM. Excel inclui filtro, primeira linha congelada e largura ajustada.

## Uso além do computador local

O modo local vincula o servidor a `127.0.0.1`. A autenticação, quotas e separação de contas são implementadas, mas uma implantação comercial pública também precisa de HTTPS (`COOKIE_SECURE=true`), proxy reverso bem configurado, backups, monitoramento, proteção contra abuso/automação de cadastros e integração real do checkout. O estado das tarefas está em memória e foi pensado para um único processo; não inicie múltiplos workers sem mover tarefas/sessões de busca para uma fila/estado compartilhado. SQLite atende bem ao protótipo local; dimensione e valide a infraestrutura antes de operar com muitos clientes simultâneos.

## Testes e validação

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app.py auth.py billing.py classifier.py scraper.py
```

Inicie `uvicorn` e confirme `GET /api/health`, que retorna o status da aplicação e a fonte OSM.
