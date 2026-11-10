<?php
require_once __DIR__.'/NerdToolsHelpers.php';
header('Content-Type: application/json');
header('Cache-Control: no-store');
try {
    $manager = new NerdTools();
    $catalog = $manager->catalog(); $config = $manager->config();
    $owned = $manager->legacyOwned(); $installed = $manager->installed(); $packages = []; $migrated = [];
    foreach (($catalog['packages'] ?? []) as $name => $package) {
        $tool = $package['tool'];
        $entry = $owned[$name] ?? ($owned[$tool] ?? null);
        if (!isset($owned[$name]) && isset($owned[$tool]) && $tool !== $name) $migrated[$tool] = true;
        $current = null; $external = false;
        foreach ($installed as $record) {
            if (nt_package_name($record) !== $name && $record !== ($entry['record'] ?? null)) continue;
            $current = $record;
            if ($record !== ($entry['record'] ?? null)) $external = true;
        }
        $packages[] = [
            'name'=>$name, 'tool'=>$tool, 'file'=>$package['file'], 'version'=>$package['version'],
            'dependencies'=>$package['dependencies'], 'dependency_labels'=>array_map('nt_tool_name',$package['dependencies']),
            'size'=>format_size($package['size']), 'installed'=>$current !== null, 'current'=>$current ?? '—',
            'exact'=>$current === nt_record($package['file']), 'external'=>$external,
            'selected'=>nt_preference($config,$name) === 'yes',
        ];
    }
    foreach ($owned as $name => $entry) {
        if (isset($catalog['packages'][$name]) || isset($migrated[$name])) continue;
        $packages[] = [
            'name'=>$name, 'tool'=>$entry['tool'] ?? nt_tool_name($name), 'file'=>$entry['file'], 'version'=>'(unavailable)',
            'dependencies'=>$entry['dependencies'] ?? [], 'dependency_labels'=>array_map('nt_tool_name',$entry['dependencies'] ?? []),
            'size'=>'—', 'installed'=>in_array($entry['record'],$installed,true), 'current'=>$entry['record'],
            'exact'=>false, 'external'=>false, 'unavailable'=>true, 'selected'=>nt_preference($config,$name) === 'yes',
        ];
    }
    echo json_encode(['packages'=>$packages, 'external_packages'=>$catalog['external_packages'] ?? [],
        'provided_packages'=>$catalog['provided_packages'] ?? [], 'release'=>$catalog['release'] ?? '',
        'selector'=>$catalog['selector'] ?? 'latest', 'releases'=>$catalog['releases'] ?? [],
        'state'=>$manager->state(), 'hours'=>$manager->interval()], JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    http_response_code(400);
    echo json_encode(['error'=>$error->getMessage()]);
}
