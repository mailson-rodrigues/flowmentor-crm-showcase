<?php
/**
 * Flowmentor CRM — lead capture from a web form (PHP 8+, no dependencies).
 *
 * Publishes the lead to POST /api/v1/public/leads using an API key with the scopes
 * `contacts:write` and `deals:write` (Settings > Webhooks & API > API keys).
 *
 * Environment variables (never put the key in HTML/JS):
 *   FLOWMENTOR_URL      e.g. https://crm.yourcompany.com
 *   FLOWMENTOR_API_KEY  e.g. fm_xxxxxxxxxxxxxxxx
 */
declare(strict_types=1);

const FIELD_LIMITS = ['name' => 150, 'phone' => 20, 'email' => 150, 'company' => 200, 'interest' => 200, 'message' => 3000];

function clean(string $v, int $max): string
{
    return mb_substr(trim(strip_tags($v)), 0, $max);
}

function respond(int $status, array $body): never
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($body, JSON_UNESCAPED_UNICODE);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    respond(405, ['error' => 'Método não permitido']);
}
if (!empty($_POST['website'])) {            // anti-bot honeypot: hidden field that humans do not fill in
    respond(200, ['ok' => true]);
}

$data = [];
foreach (FIELD_LIMITS as $field => $max) {
    $data[$field] = clean((string)($_POST[$field] ?? ''), $max);
}
if ($data['name'] === '' || !preg_match('/^\+?[\d\s().-]{10,20}$/', $data['phone'])) {
    respond(422, ['error' => 'Informe nome e um telefone válido (DDD + número).']);
}
if ($data['email'] !== '' && !filter_var($data['email'], FILTER_VALIDATE_EMAIL)) {
    respond(422, ['error' => 'E-mail inválido.']);
}
if (($_POST['consent'] ?? '') !== '1') {    // LGPD, art. 7, I: explicit consent of the data subject
    respond(422, ['error' => 'É necessário aceitar o tratamento dos dados para continuar.']);
}

$payload = array_filter($data, static fn($v) => $v !== '') + ['source' => 'site', 'consent' => true];
$ch = curl_init(rtrim((string)getenv('FLOWMENTOR_URL'), '/') . '/api/v1/public/leads');
curl_setopt_array($ch, [
    CURLOPT_POST           => true,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT        => 10,
    CURLOPT_HTTPHEADER     => ['Content-Type: application/json', 'X-API-Key: ' . getenv('FLOWMENTOR_API_KEY'), 'X-Forwarded-For: ' . ($_SERVER['REMOTE_ADDR'] ?? '')],
    CURLOPT_POSTFIELDS     => json_encode($payload, JSON_UNESCAPED_UNICODE),
    CURLOPT_SSL_VERIFYPEER => true,
]);
$resp = curl_exec($ch);
$code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

if ($resp === false || $code >= 400) {
    error_log("Flowmentor lead falhou (HTTP $code): " . substr((string)$resp, 0, 300));
    respond(502, ['error' => 'Não foi possível registrar seu contato agora. Tente novamente em instantes.']);
}
respond(201, ['ok' => true, 'message' => 'Recebemos seu contato! Em breve falaremos com você.']);
