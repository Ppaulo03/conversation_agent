# Futuro: visão (imagens)

> **Status: FUTURO. Nada disto está implementado e não há fase atribuída.** Registra a decisão de
> desenho para quando for priorizado. Hoje uma imagem chega ao agente apenas como `[image received]`
> (nomeada, nunca descrita) e, com a fase 15b, pode ser repassada a uma tool por handle.

## 1. Duas formas de dar visão ao agente

| | A. Descritor separado (começar por aqui) | B. Modelo principal multimodal |
|---|---|---|
| Ideia | um modelo de visão transforma a imagem em texto e o agente lê o texto | a imagem vai direto ao modelo do agente, como parte da mensagem |
| Funciona com modelo principal só de texto | **sim** | não: exige trocar o modelo principal |
| Encaixe no que existe | mesmo desenho do `Transcriber`: um passo journalado, custo no livro de uso | muda o contrato central do LLM (partes de imagem em `LLMRequest`) e todos os adaptadores |
| Replay e custo | o replay reutiliza a descrição, sem repagar | a imagem iria de novo a cada chamada do turno e pelo histórico |
| Controle | dá para limitar, rotular e revisar o que o agente "viu" | o modelo vê tudo |
| Limite | o agente só enxerga o que a descrição disse | entendimento mais rico |

A opção B só compensa quando o modelo principal for multimodal e houver necessidade de raciocinar
diretamente sobre a imagem. Fica como evolução posterior.

## 2. Desenho da opção A

- **Porta `ImageDescriber`** ao lado de `Transcriber`, com um adaptador para o protocolo OpenAI de chat
  com imagem. Cobre qualquer provedor que ofereça um modelo de visão no mesmo endpoint e servidores locais
  (vLLM, Ollama): a escolha é configuração, como no STT (`STT_*` -> um conjunto `VISION_*`).
- **Duas chaves, padrão desligado:** o operador configura o descritor E o agente declara `vision: on`
  (padrão `off`). Uma foto pode ter rosto, documento ou endereço; com `off` a imagem só é nomeada e nem é
  baixada.
- **O que o agente lê:** algo como `[image, automatic description: ...]`. O rótulo evita tratar o texto como
  fato certo, e a descrição é dado, nunca instrução.
- **Texto dentro da imagem** (comprovante, print de erro, placa) é o caso de maior valor: o prompt do
  descritor pede descrição objetiva e transcrição do texto visível, e diz que qualquer instrução escrita na
  imagem é conteúdo, não ordem.
- **Custo:** o livro de uso já conta tokens; a chamada entra com `purpose="vision"`, sem coluna nova.
- **Limites:** bytes por imagem, formatos (jpeg, png, webp), teto de imagens descritas por mensagem (as
  demais continuam nomeadas), tamanho máximo da descrição.
- **Falha:** nunca derruba o turno; o agente recebe "imagem que não pôde ser descrita".
- **Com a 15b:** o handle continua valendo (descrever e também anexar a um chamado).

## 3. Privacidade

- A descrição fica no histórico da conversa: mesma retenção e mesmo apagamento do restante do estado.
- Com um descritor em nuvem a imagem sai da infraestrutura: o guia deve dizer isso explicitamente. Um modelo
  local mantém tudo dentro de casa.

## 4. Fora do primeiro corte

PDF e documentos (extração de texto é outra peça), vídeo e GIF animado, redimensionamento de imagens muito
grandes, a opção B.

## 5. Perguntas em aberto

1. Modelo padrão do descritor: configurável sem padrão (com um exemplo na documentação) ou um padrão por
   provedor?
2. OCR é requisito dos casos de uso reais (comprovante, print de erro)? Define o prompt e os testes.
3. Teto de imagens descritas por mensagem (proposta: 3).
