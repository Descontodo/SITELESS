SITLESS DESKTOP — VERSÃO LOCAL

O aplicativo roda no seu computador e abre a interface no navegador padrão.
O servidor escuta somente em 127.0.0.1. Seus dados ficam na pasta local do
Siteless e não são enviados para um servidor central.

WINDOWS
1. Extraia a pasta Siteless inteira.
2. Abra Siteless.exe.
3. Deixe a janela aberta enquanto usa o aplicativo. Feche-a para encerrar.

MACOS
1. Extraia a pasta Siteless inteira.
2. Abra Iniciar-Siteless.command no Terminal.
3. Se necessário, no Terminal execute chmod +x Iniciar-Siteless.command e tente novamente.
4. Se o macOS bloquear o arquivo, use clique direito > Abrir e confirme.
5. Deixe o Terminal aberto enquanto usa o aplicativo.

LINUX
1. Extraia a pasta Siteless inteira.
2. Abra um terminal nessa pasta e execute ./iniciar-siteless.sh.
3. Deixe o terminal aberto enquanto usa o aplicativo.

No primeiro uso, o sistema cria o administrador local conforme ADMIN_EMAIL e
ADMIN_PASSWORD definidos no ambiente. Sem configuração, os padrões do projeto
são admin@sistema.com e Admin@1234. Troque a senha antes de expor o serviço.

Os dados permanecem locais. Faça backup do arquivo leads_sem_site.db com o
Siteless fechado. Google Drive pode guardar cópias e exportações, mas não deve
ser usado para sincronizar o banco ativo entre computadores.

Esta distribuição local é para uso individual e validação. Checkout Stripe,
contas compartilhadas e quotas SaaS precisam da versão hospedada; não ofereça
esta pasta como o produto SaaS final.
