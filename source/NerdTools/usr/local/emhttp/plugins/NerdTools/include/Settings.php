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
    $manager = new NerdTools(); $action = $_POST['action'] ?? ''; $response = ['ok'=>true];
    if ($action === 'plan') {
        // Read-only previews may overlap; confirmation recomputes the plan while holding the mutation lock.
        $response = $manager->plan($_POST['actions'] ?? [], $_POST['update_check_hours'] ?? '24');
    } else {
        $lock = $manager->lock();
        if (in_array($action, ['refresh', 'release'], true)) {
            $selector = ($action === 'release') ? ($_POST['release'] ?? '') : null;
            if (!$manager->refresh(true, $selector)) throw new RuntimeException('GitHub rate limit is still active; retry after the displayed reset time');
        } elseif ($action === 'confirm') {
            $response['plan'] = $manager->confirmPlan($_POST['plan'] ?? '', $_POST['actions'] ?? [],
                $_POST['update_check_hours'] ?? '24');
        } elseif ($action === 'token') {
            $manager->saveToken(trim((string)($_POST['github_token'] ?? '')));
        } else throw new RuntimeException('Unknown action');
    }
    echo json_encode($response, JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    if (http_response_code() < 400) http_response_code(400);
    echo json_encode(['error'=>$error->getMessage()]);
} finally { if (isset($lock) && is_resource($lock)) fclose($lock); }
