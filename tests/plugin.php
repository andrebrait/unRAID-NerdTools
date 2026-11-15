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
$removeOwned = [
    'dep'=>['record'=>'dep-1.0-x86_64-1','dependencies'=>[]],
    'app'=>['record'=>'app-1.0-x86_64-1','dependencies'=>['dep']],
];
$removeInstalled = array_column($removeOwned,'record');
$removeConfig = ['dep'=>'yes','app'=>'yes','update_check_hours'=>'24'];
rejects(function() use ($planPackages,$removeOwned,$removeInstalled,$removeConfig) {
    nt_plan($planPackages,$removeOwned,$removeInstalled,$removeConfig,['dep'=>'remove'],24);
});
$plan = nt_plan($planPackages,$removeOwned,$removeInstalled,$removeConfig,['dep'=>'remove','app'=>'remove'],24);
check(array_column($plan['changes'],'package') === ['app','dep'], 'Dependents are removed before dependencies');
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
