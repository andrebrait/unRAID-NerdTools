<?php
require_once __DIR__.'/NerdToolsHelpers.php';
header('Content-Type: application/json');
header('Cache-Control: no-store');
try {
    $vars = @parse_ini_file('/var/local/emhttp/var.ini', false, INI_SCANNER_RAW);
    $token = $_POST['csrf_token'] ?? null;
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST' || !is_string($token) || empty($vars['csrf_token'])
        || !hash_equals($vars['csrf_token'], $token)) {
        http_response_code(403);
        throw new RuntimeException('Invalid request; reload the page');
    }
    $manager = new NerdTools();
    $lock = $manager->lock();
    if (in_array($_POST['action'] ?? '', ['refresh', 'release'], true)) {
        $selector = ($_POST['action'] === 'release') ? ($_POST['release'] ?? '') : null;
        if (!$manager->refresh(true, $selector)) throw new RuntimeException('GitHub rate limit is still active; retry after the displayed reset time');
    } elseif (($_POST['action'] ?? '') === 'save') {
        $manager->save($_POST['packages'] ?? [], $_POST['update_check_hours'] ?? '24');
    } else throw new RuntimeException('Unknown action');
    echo json_encode(['ok'=>true], JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    if (http_response_code() < 400) http_response_code(400);
    echo json_encode(['error'=>$error->getMessage()]);
} finally { if (isset($lock) && is_resource($lock)) fclose($lock); }
