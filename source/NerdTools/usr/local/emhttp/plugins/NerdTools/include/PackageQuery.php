<?php
require_once __DIR__.'/NerdToolsHelpers.php';
header('Content-Type: application/json');
header('Cache-Control: no-store');
try {
    $manager = new NerdTools();
    $catalog = $manager->catalog(); $config = $manager->config();
    $owned = $manager->legacyOwned(); $installed = $manager->installed(); $packages = []; $migrated = [];
    foreach (($catalog['packages'] ?? []) as $name => $package) {
        $tool = $package['tool']; $ownedName = isset($owned[$name]) ? $name : $tool;
        $entry = $owned[$name] ?? ($owned[$tool] ?? null);
        if (!isset($owned[$name]) && isset($owned[$tool]) && $tool !== $name) $migrated[$tool] = true;
        $current = null; $external = false;
        foreach ($installed as $record) {
            if (nt_package_name($record) !== $name && $record !== ($entry['record'] ?? null)) continue;
            $current = $record;
            if ($record !== ($entry['record'] ?? null)) $external = true;
        }
        $installedVersion = $current === null ? null : nt_package_version($current, $current === ($entry['record'] ?? null) ? $ownedName : $name);
        $exact = $current === nt_record($package['file']);
        $packageActions = nt_package_actions($current,$ownedName,$package,$external,$entry !== null);
        $updateKind = $packageActions['update_kind']; $actions = $packageActions['actions'];
        $selected = nt_preference($config,$name) === 'yes';
        $packages[] = [
            'name'=>$name, 'tool'=>$tool, 'file'=>$package['file'], 'version'=>$package['version'],
            'dependencies'=>$package['dependencies'], 'dependency_labels'=>array_map('nt_tool_name',$package['dependencies']),
            'size'=>format_size($package['size']), 'installed'=>$current !== null, 'current'=>$current ?? '—',
            'installed_version'=>$installedVersion, 'available_version'=>$package['version'],
            'exact'=>$exact, 'external'=>$external, 'selected'=>$selected, 'update_kind'=>$updateKind,
            'default_action'=>(!$external && $current === null && $selected) ? 'install' : 'keep', 'actions'=>$actions,
        ];
    }
    foreach ($owned as $name => $entry) {
        if (isset($catalog['packages'][$name]) || isset($migrated[$name])) continue;
        $isInstalled = in_array($entry['record'],$installed,true);
        $packages[] = [
            'name'=>$name, 'tool'=>$entry['tool'] ?? nt_tool_name($name), 'file'=>$entry['file'], 'version'=>'(unavailable)',
            'dependencies'=>$entry['dependencies'] ?? [], 'dependency_labels'=>array_map('nt_tool_name',$entry['dependencies'] ?? []),
            'size'=>'—', 'installed'=>$isInstalled, 'current'=>$entry['record'],
            'installed_version'=>$isInstalled ? nt_package_version($entry['record'],$name) : null, 'available_version'=>null,
            'exact'=>false, 'external'=>false, 'unavailable'=>true, 'selected'=>nt_preference($config,$name) === 'yes',
            'update_kind'=>null, 'default_action'=>'keep', 'actions'=>['keep','remove'],
        ];
    }
    echo json_encode(['packages'=>$packages, 'external_packages'=>$catalog['external_packages'] ?? [],
        'provided_packages'=>$catalog['provided_packages'] ?? [], 'release'=>$catalog['release'] ?? '',
        'selector'=>$catalog['selector'] ?? 'latest', 'releases'=>$catalog['releases'] ?? [],
        'state'=>$manager->state(), 'hours'=>$manager->interval(), 'token_set'=>$manager->token() !== ''], JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    http_response_code(400);
    echo json_encode(['error'=>$error->getMessage()]);
}
