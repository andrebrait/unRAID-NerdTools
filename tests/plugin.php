<?php
// Run with php tests/plugin.php; all state is isolated below a temporary directory.
require __DIR__.'/../source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php';
function check($value, $message) { if (!$value) throw new RuntimeException($message); }
function rejects($fn) { try { $fn(); } catch (RuntimeException $e) { return; } throw new RuntimeException('Expected refusal'); }
$root = sys_get_temp_dir().'/nerdtools-test-'.bin2hex(random_bytes(6));
mkdir($root);
$pkg = ['name'=>'tool','version'=>'1.2','file'=>'tool-1.2-x86_64-1.txz','sha256'=>str_repeat('a',64),'size'=>123,'dependencies'=>[]];
$catalog = ['family'=>'7.3','min_unraid'=>'7.3.4','packages'=>[$pkg]];
$asset = ['name'=>$pkg['file'],'size'=>123,'browser_download_url'=>'https://github.com/andrebrait/unRAID-NerdTools/releases/download/test/'.$pkg['file']];
$validated = nt_validate_catalog($catalog, [$asset], '7.3.4');
check(isset($validated['packages']['tool']), 'Catalog has indexed package');
$providedCatalog = $catalog;
$providedCatalog['provided_packages'] = [['name'=>'liburing','version'=>'2.15','provider'=>'unraid','min_unraid'=>'7.3.4']];
$provided = nt_validate_catalog($providedCatalog,[$asset],'7.3.4');
check($provided['provided_packages'][0]['name'] === 'liburing' && !isset($provided['packages']['liburing']), 'Built-in capability is not an installable package');
$providedCatalog['provided_packages'][0]['file'] = 'fake.txz';
rejects(function() use ($providedCatalog,$asset) { nt_validate_catalog($providedCatalog,[$asset],'7.3.4'); });
rejects(function() use ($catalog,$asset) { nt_validate_catalog($catalog,[$asset],'7.4.0'); });
// A later family is served from the first verified prerelease onward, never earlier or across families.
$next = array_merge($catalog, ['family'=>'7.4','min_unraid'=>'7.4.0-rc.2']);
foreach (['7.4.0-rc.2','7.4.0-rc.3','7.4.0','7.4.1'] as $running) nt_validate_catalog($next,[$asset],$running);
foreach (['7.4.0-rc.1','7.4.0-beta.5','7.3.4','7.5.0','7.4.0-foo'] as $running)
    rejects(function() use ($next,$asset,$running) { nt_validate_catalog($next,[$asset],$running); });
rejects(function() use ($catalog,$asset) { nt_validate_catalog($catalog,[$asset],'7.3.3'); });
rejects(function() use ($catalog) { nt_validate_catalog($catalog,[],'7.3.4'); });
$bad = $catalog; $bad['packages'][0]['file'] = '../tool.txz';
rejects(function() use ($bad,$asset) { nt_validate_catalog($bad,[$asset],'7.3.4'); });
$bad = $catalog; $bad['packages'][0]['sha256'] = 'bogus';
rejects(function() use ($bad,$asset) { nt_validate_catalog($bad,[$asset],'7.3.4'); });
$bad = $catalog; $bad['packages'][0]['dependencies'] = ['missing'];
rejects(function() use ($bad,$asset) { nt_validate_catalog($bad,[$asset],'7.3.4'); });
$bad = $catalog; $bad['packages'][0]['dependencies'] = ['tool'];
rejects(function() use ($bad,$asset) { nt_validate_catalog($bad,[$asset],'7.3.4'); });
$packages = $validated['packages'];
$packages['dep'] = array_merge($packages['tool'], ['name'=>'dep','dependencies'=>[]]);
$packages['tool']['dependencies'] = ['dep'];
check(nt_resolve(['tool'], $packages) === ['dep','tool'], 'Dependency-first order');
check(nt_preference(['tool-1__1'=>'yes'], 'tool') === 'yes', 'Legacy version preference retained');
check(nt_preference(['tool-1__1'=>'yes','tool'=>'no'], 'tool') === 'no', 'Stable preference wins');
check(nt_preference(['tool-extra-1__1'=>'yes'], 'tool') === 'no', 'No package prefix collision');
check(nt_next_check(1000, 24, false, 0) === 87400, 'Daily default interval');
check(nt_next_check(1000, 24, true, 0) === 4600, 'Hourly failure retry');
check(nt_next_check(1000, 24, true, 9000) === 9000, 'Rate limit reset wins');
check(nt_package_version('nerdtools-tool-1.2.3-x86_64-1', 'nerdtools-tool') === '1.2.3', 'Installed version extracted');
check(nt_package_version('other-1.2.3-x86_64-1', 'nerdtools-tool') === null, 'Mismatched package version refused');
check(nt_package_display_version('nerdtools-vim-9.2.1252-x86_64-2_NT73','nerdtools-vim') === '9.2.1252-2_NT73',
    'Full NerdTools package version includes build revision');
$planPackage = function($name, $version, $dependencies = []) {
    return ['name'=>$name, 'tool'=>$name, 'version'=>$version,
        'file'=>$name.'-'.$version.'-x86_64-1.txz', 'dependencies'=>$dependencies];
};
$planPackages = [
    'dep'=>$planPackage('dep','2.0'),
    'app'=>$planPackage('app','1.0',['dep']),
    'consumer'=>$planPackage('consumer','2.0',['dep']),
    'other'=>$planPackage('other','2.0'),
    'fresh'=>$planPackage('fresh','1.0'),
    'newapp'=>$planPackage('newapp','1.0',['fresh']),
];
$planOwned = [
    'dep'=>['record'=>'dep-1.0-x86_64-1','dependencies'=>[]],
    'consumer'=>['record'=>'consumer-1.0-x86_64-1','dependencies'=>['dep']],
    'other'=>['record'=>'other-1.0-x86_64-1','dependencies'=>[]],
];
$planInstalled = array_column($planOwned, 'record');
$planConfig = ['dep'=>'yes','consumer'=>'yes','other'=>'yes','update_check_hours'=>'24'];
$plan = nt_plan($planPackages,$planOwned,$planInstalled,$planConfig,[],24);
check($plan['changes'] === [] && preg_match('/^[a-f0-9]{64}$/D',$plan['id']), 'Outdated selected packages remain unchanged');
$plan = nt_plan($planPackages,$planOwned,$planInstalled,$planConfig,['other'=>'upgrade'],24);
check(array_column($plan['changes'],'package') === ['other'] && $plan['changes'][0]['change'] === 'upgrade', 'Explicit upgrade changes only its package');
$plan = nt_plan($planPackages,$planOwned,$planInstalled,$planConfig,['app'=>'install'],24);
check(array_column($plan['changes'],'package') === ['dep','app','consumer'], 'New install upgrades stale dependency and installed dependents only');
check($plan['rows']['dep']['automatic'] && $plan['rows']['dep']['reason'] === 'Required by app', 'Required dependency upgrade is explained');
check(!in_array('other',array_column($plan['changes'],'package'),true), 'Unrelated update is excluded');
$plan = nt_plan($planPackages,$planOwned,$planInstalled,$planConfig,['newapp'=>'install'],24);
check(array_column($plan['changes'],'package') === ['fresh','newapp'], 'Missing dependency installs before requested package');
$unselectedPlanId = $plan['id'];
$selectedConfig = $planConfig; $selectedConfig['newapp'] = 'yes';
$selectedPlan = nt_plan($planPackages,$planOwned,$planInstalled,$selectedConfig,['newapp'=>'install'],24);
check($selectedPlan['id'] !== $unselectedPlanId, 'Plan fingerprint includes current preferences');
$olderPackages = [
    'dep'=>$planPackage('dep','1.0'),
    'app'=>$planPackage('app','1.0',['dep']),
    'consumer'=>$planPackage('consumer','1.0',['dep']),
];
$newerOwned = [
    'dep'=>['record'=>'dep-2.0-x86_64-1','dependencies'=>[]],
    'consumer'=>['record'=>'consumer-2.0-x86_64-1','dependencies'=>['dep']],
];
rejects(function() use ($olderPackages,$newerOwned) {
    nt_plan($olderPackages,$newerOwned,array_column($newerOwned,'record'),
        ['dep'=>'yes','consumer'=>'yes','update_check_hours'=>'24'],['app'=>'install'],24);
});
$downgradePlan = nt_plan($olderPackages,$newerOwned,array_column($newerOwned,'record'),
    ['dep'=>'yes','consumer'=>'yes','update_check_hours'=>'24'],
    ['app'=>'install','dep'=>'downgrade','consumer'=>'downgrade'],24);
check(array_column($downgradePlan['changes'],'package') === ['dep','app','consumer']
    && !$downgradePlan['rows']['dep']['automatic'] && !$downgradePlan['rows']['consumer']['automatic'],
    'Explicit dependency and dependent downgrades may accompany an install');
$unavailableOwned = [
    'dep'=>['record'=>'dep-1.0-x86_64-1','dependencies'=>[]],
    'consumer'=>['record'=>'consumer-1.0-x86_64-1','dependencies'=>['dep']],
];
$availablePackages = ['dep'=>$planPackage('dep','2.0'),'app'=>$planPackage('app','1.0',['dep'])];
rejects(function() use ($availablePackages,$unavailableOwned) {
    nt_plan($availablePackages,$unavailableOwned,array_column($unavailableOwned,'record'),
        ['dep'=>'yes','consumer'=>'yes','update_check_hours'=>'24'],['app'=>'install'],24);
});
$keptPackages = [
    'dep'=>$planPackage('dep','1.0'),
    'consumer'=>$planPackage('consumer','2.0'),
];
$keptOwned = [
    'dep'=>['record'=>'dep-1.0-x86_64-1','dependencies'=>[]],
    'consumer'=>['record'=>'consumer-1.0-x86_64-1','dependencies'=>['dep']],
];
$keptConfig = ['dep'=>'yes','consumer'=>'yes','update_check_hours'=>'24'];
$keptRemoval = nt_plan($keptPackages,$keptOwned,array_column($keptOwned,'record'),$keptConfig,['dep'=>'remove'],24);
check(array_column($keptRemoval['changes'],'package') === ['consumer','dep']
    && $keptRemoval['rows']['consumer']['automatic'] && $keptRemoval['rows']['consumer']['change'] === 'remove',
    'Kept installed dependent follows dependency removal');
$replacementPlan = nt_plan($keptPackages,$keptOwned,array_column($keptOwned,'record'),$keptConfig,
    ['dep'=>'remove','consumer'=>'upgrade'],24);
check(array_column($replacementPlan['changes'],'package') === ['consumer','dep'],
    'Replacement uses catalog dependencies while kept package uses owned dependencies');
$externalPackages = ['dep'=>$planPackage('dep','1.0'),'app'=>$planPackage('app','1.0',['dep'])];
rejects(function() use ($externalPackages) {
    nt_plan($externalPackages,[],['dep-1.0-x86_64-1'],
        ['dep'=>'no','app'=>'no','update_check_hours'=>'24'],['app'=>'install'],24);
});
$expandedPackages = [
    'dep'=>$planPackage('dep','2.0'),
    'app'=>$planPackage('app','1.0',['dep']),
    'extra'=>$planPackage('extra','1.0'),
    'consumer'=>$planPackage('consumer','2.0',['dep','extra']),
];
$expandedOwned = [
    'dep'=>['record'=>'dep-1.0-x86_64-1','dependencies'=>[]],
    'consumer'=>['record'=>'consumer-1.0-x86_64-1','dependencies'=>['dep']],
];
$expandedPlan = nt_plan($expandedPackages,$expandedOwned,array_column($expandedOwned,'record'),
    ['dep'=>'yes','consumer'=>'yes','update_check_hours'=>'24'],['app'=>'install'],24);
check(array_column($expandedPlan['changes'],'package') === ['dep','app','extra','consumer']
    && $expandedPlan['rows']['extra']['change'] === 'install',
    'Automatically upgraded dependent stages its new dependencies');
$upgradeCascade = nt_plan($expandedPackages,$expandedOwned,array_column($expandedOwned,'record'),
    ['dep'=>'yes','consumer'=>'yes','extra'=>'no','update_check_hours'=>'24'],['dep'=>'upgrade'],24);
check(array_column($upgradeCascade['changes'],'package') === ['dep','extra','consumer']
    && $upgradeCascade['rows']['consumer']['automatic'] && $upgradeCascade['rows']['consumer']['change'] === 'upgrade',
    'Explicit dependency upgrade stages dependent upgrades and their new dependencies');
$vimPackages = [
    'tcl'=>$planPackage('tcl','2.0'),
    'vim'=>$planPackage('vim','2.0',['tcl']),
];
$vimOwned = [
    'tcl'=>['record'=>'tcl-1.0-x86_64-1','dependencies'=>[]],
    'vim'=>['record'=>'vim-1.0-x86_64-1','dependencies'=>['tcl']],
];
$vimPlan = nt_plan($vimPackages,$vimOwned,array_column($vimOwned,'record'),
    ['tcl'=>'yes','vim'=>'yes','update_check_hours'=>'24'],['vim'=>'upgrade'],24);
check(array_column($vimPlan['changes'],'package') === ['tcl','vim']
    && $vimPlan['rows']['tcl']['automatic'] && $vimPlan['rows']['tcl']['change'] === 'upgrade',
    'Explicit upgrade stages stale installed dependencies');
$removalOwned = $expandedOwned + ['extra'=>['record'=>'extra-1.0-x86_64-1','dependencies'=>[]]];
$removalCascade = nt_plan($expandedPackages,$removalOwned,array_column($removalOwned,'record'),
    ['dep'=>'yes','consumer'=>'yes','extra'=>'yes','update_check_hours'=>'24'],
    ['app'=>'install','extra'=>'remove'],24);
$removalNames = array_column($removalCascade['changes'],'package'); sort($removalNames,SORT_STRING);
check($removalNames === ['app','consumer','dep','extra']
    && $removalCascade['rows']['consumer']['automatic'] && $removalCascade['rows']['consumer']['change'] === 'remove',
    'Removing a dependency stages removal of installed dependents');
$removedDependentPlan = nt_plan($expandedPackages,$expandedOwned,array_column($expandedOwned,'record'),
    ['dep'=>'yes','consumer'=>'yes','extra'=>'no','update_check_hours'=>'24'],
    ['app'=>'install','consumer'=>'remove'],24);
check(array_column($removedDependentPlan['changes'],'package') === ['dep','app','consumer'],
    'Explicitly removed dependent is not upgraded and does not stage new dependencies');
$sameBuild = $planPackage('tool','1.0'); $sameBuild['file'] = 'tool-1.0-x86_64-2.txz';
check(nt_update_kind('tool-1.0-x86_64-1','tool',$sameBuild) === 'upgrade',
    'Same-version package build replacement is an upgrade action');
$sameBuildPlan = nt_plan(['tool'=>$sameBuild],
    ['tool'=>['record'=>'tool-1.0-x86_64-1','dependencies'=>[]]],['tool-1.0-x86_64-1'],
    ['tool'=>'yes','update_check_hours'=>'24'],['tool'=>'upgrade'],24);
check($sameBuildPlan['changes'][0]['change'] === 'upgrade', 'Same-version package build replacement can be planned');
$legacyPackage = $planPackage('nerdtools-tool','1.0'); $legacyPackage['tool'] = 'tool';
$legacyRecord = 'tool-1.0-x86_64-1_nerdtools';
check(nt_update_kind($legacyRecord,'tool',$legacyPackage) === 'upgrade',
    'Same-version legacy migration is offered as an upgrade action');
$legacyActions = nt_package_actions($legacyRecord,'tool',$legacyPackage,false,true);
check($legacyActions['update_kind'] === 'upgrade' && in_array('upgrade',$legacyActions['actions'],true),
    'Legacy migration query response offers Upgrade');
$legacyPlan = nt_plan(['nerdtools-tool'=>$legacyPackage],
    ['tool'=>['record'=>$legacyRecord,'dependencies'=>[]]],[$legacyRecord],
    ['tool'=>'yes','update_check_hours'=>'24'],['nerdtools-tool'=>'upgrade'],24);
check($legacyPlan['changes'][0]['package'] === 'nerdtools-tool'
    && $legacyPlan['changes'][0]['change'] === 'upgrade', 'Same-version legacy migration can be planned');
$uninstalledActions = nt_package_actions(null,'screen',$planPackage('screen','1.0'),false,false);
check($uninstalledActions['update_kind'] === null && $uninstalledActions['actions'] === ['keep','install'],
    'Uninstalled package never reports an available update');
$namespacedDep = $planPackage('nerdtools-dep','1.0'); $namespacedDep['tool'] = 'dep';
$namespacedConsumer = $planPackage('nerdtools-consumer','1.0',['nerdtools-dep']); $namespacedConsumer['tool'] = 'consumer';
$legacyOwned = [
    'dep'=>['record'=>'dep-1.0-x86_64-1_nerdtools','dependencies'=>[]],
    'consumer'=>['record'=>'consumer-1.0-x86_64-1_nerdtools','dependencies'=>['dep']],
];
$namespacePlan = nt_plan(['nerdtools-dep'=>$namespacedDep,'nerdtools-consumer'=>$namespacedConsumer],
    $legacyOwned,array_column($legacyOwned,'record'),
    ['dep'=>'yes','consumer'=>'yes','update_check_hours'=>'24'],[],24);
check($namespacePlan['changes'] === [], 'Legacy dependency names resolve through namespace migration');
$removeOwned = [
    'dep'=>['record'=>'dep-1.0-x86_64-1','dependencies'=>[]],
    'app'=>['record'=>'app-1.0-x86_64-1','dependencies'=>['dep']],
];
$removeInstalled = array_column($removeOwned,'record');
$removeConfig = ['dep'=>'yes','app'=>'yes','update_check_hours'=>'24'];
$plan = nt_plan($planPackages,$removeOwned,$removeInstalled,$removeConfig,['dep'=>'remove'],24);
check(array_column($plan['changes'],'package') === ['app','dep']
    && $plan['rows']['app']['automatic'] && $plan['rows']['app']['change'] === 'remove',
    'Dependency removal automatically selects installed dependents for removal');
check($plan['changes'][0]['from'] === '1.0' && $plan['changes'][0]['to'] === null, 'Removal preview ends with no installed version');
$chainPackages = [
    'dep'=>$planPackage('dep','1.0'),
    'app'=>$planPackage('app','1.0',['dep']),
    'peer'=>$planPackage('peer','1.0',['dep']),
    'top'=>$planPackage('top','1.0',['app']),
];
$chainOwned = [
    'dep'=>['record'=>'dep-1.0-x86_64-1','dependencies'=>[]],
    'app'=>['record'=>'app-1.0-x86_64-1','dependencies'=>['dep']],
    'peer'=>['record'=>'peer-1.0-x86_64-1','dependencies'=>['dep']],
    'top'=>['record'=>'top-1.0-x86_64-1','dependencies'=>['app']],
];
$chainPlan = nt_plan($chainPackages,$chainOwned,array_column($chainOwned,'record'),
    ['dep'=>'yes','app'=>'yes','peer'=>'yes','top'=>'yes','update_check_hours'=>'24'],['dep'=>'remove'],24);
check(array_column($chainPlan['changes'],'package') === ['top','peer','app','dep']
    && $chainPlan['rows']['top']['reason'] === 'Dependency dep marked for removal',
    'Dependency removal cascades with display-name removal reason');
$plan = nt_plan($planPackages,$planOwned,$planInstalled,$planConfig,[],168);
check(count($plan['changes']) === 1 && $plan['changes'][0]['change'] === 'settings', 'Settings-only change is planned');
rejects(function() use ($planPackages,$planOwned,$planInstalled,$planConfig) {
    nt_plan($planPackages,$planOwned,$planInstalled,$planConfig,['other'=>'surprise'],24);
});
file_put_contents($root.'/cache.json', '{"old":true}');
nt_atomic_write($root.'/cache.json', '{"new":true}');
check(json_decode(file_get_contents($root.'/cache.json'),true)['new'], 'Atomic cache replacement');
file_put_contents($root.'/payload', 'verified bytes');
check(nt_verified($root.'/payload', ['size'=>14,'sha256'=>hash('sha256','verified bytes')]), 'Valid digest accepted');
check(!nt_verified($root.'/payload', ['size'=>14,'sha256'=>str_repeat('0',64)]), 'Bad digest rejected');
foreach (glob($root.'/*') as $file) unlink($file);
rmdir($root);
echo "Plugin contracts passed\n";
