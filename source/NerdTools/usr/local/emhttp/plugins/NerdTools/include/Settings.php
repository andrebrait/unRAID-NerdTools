<?php
require_once __DIR__.'/NerdToolsHelpers.php';
header('Content-Type: application/json');
header('Cache-Control: no-store');
try {
    // Unraid's PHP auto_prepend (webGui/include/local_prepend.php) verifies the CSRF token of every
    // POST and then removes it from $_POST, so it must not be checked again here.
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
        http_response_code(405);
        throw new RuntimeException('Invalid request; reload the page');
    }
    $manager = new NerdTools();
    $lock = $manager->lock(); $response = ['ok'=>true];
    if (in_array($_POST['action'] ?? '', ['refresh', 'release'], true)) {
        $selector = ($_POST['action'] === 'release') ? ($_POST['release'] ?? '') : null;
        if (!$manager->refresh(true, $selector)) throw new RuntimeException('GitHub rate limit is still active; retry after the displayed reset time');
    } elseif (($_POST['action'] ?? '') === 'plan') {
        $response = $manager->plan($_POST['actions'] ?? [], $_POST['update_check_hours'] ?? '24');
    } elseif (($_POST['action'] ?? '') === 'confirm') {
        $response['plan'] = $manager->confirmPlan($_POST['plan'] ?? '', $_POST['actions'] ?? [],
            $_POST['update_check_hours'] ?? '24');
    } elseif (($_POST['action'] ?? '') === 'token') {
        $manager->saveToken(trim((string)($_POST['github_token'] ?? '')));
    } else throw new RuntimeException('Unknown action');
    echo json_encode($response, JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    if (http_response_code() < 400) http_response_code(400);
    echo json_encode(['error'=>$error->getMessage()]);
} finally { if (isset($lock) && is_resource($lock)) fclose($lock); }
