var nerdLoaded = false;
var nerdPlan = null;
var planRequest = null;
var planRevision = 0;
var catalogRevision = 0;
var planQueued = false;
var confirmingPlanId = null;
var displayedPlanId = null;
var displayedPlanRevision = null;
var actionLabels = {keep: 'Keep', install: 'Install', upgrade: 'Upgrade', downgrade: 'Downgrade', remove: 'Remove'};
var changeLabels = {
    'no-change': 'No change',
    'update-available': 'Update available — no change',
    'downgrade-available': 'Downgrade available — no change',
    install: 'Install',
    upgrade: 'Upgrade',
    downgrade: 'Downgrade',
    remove: 'Remove',
    external: 'Protected — no change',
    unavailable: 'Unavailable — no change',
    settings: 'Change setting'
};
$(function() {
    $('#tblPackages').tablesorter({
        sortList: [[1, 0]],
        widgets: ['saveSort', 'filter', 'stickyHeaders', 'zebra'],
        widgetOptions: {
            stickyHeaders_filteredToTop: true,
            stickyHeaders_attachTo: null,
            stickyHeaders_offset: ($('#header').css('position') === 'fixed') ? '90' : '0',
            filter_hideEmpty: true, filter_liveSearch: true, filter_saveFilters: true,
            filter_reset: '.reset',
            filter_functions: {'.filter-version': true}
        }
    });
    $('#package_form').on('submit', function(event) { event.preventDefault(); Apply(); });
    $('#btnApply').on('click', Apply);
    $('#btnRefresh').on('click', function() { packageQuery(true); });
    $('#update_check_hours').on('change', requestPlan);
    $('#btnPlanCancel').on('click', function() {
        if (canCancelConfirmation()) document.getElementById('package_plan_dialog').close();
    });
    $('#btnPlanConfirm').on('click', confirmPlan);
    $('#package_plan_dialog').on('cancel', function(event) {
        if (!canCancelConfirmation()) event.preventDefault();
    }).on('close', function() {
        displayedPlanId = null; displayedPlanRevision = null;
        $('#package_plan_error').text('').prop('hidden', true);
    });
    $('#btnRelease').on('click', function() {
        $('#btnRelease').prop('disabled', true);
        $.post('/plugins/NerdTools/include/Settings.php', {
            action: 'release', release: $('#package_release').val(),
            csrf_token: $('#package_form input[name="csrf_token"]').val()
        }).done(function() { packageError(''); packageQuery(false); })
          .fail(function(xhr) { packageError(requestError(xhr)); })
          .always(function() { $('#btnRelease').prop('disabled', false); });
    });
    function saveToken(value) {
        $('#btnToken, #btnTokenRemove').prop('disabled', true);
        $.post('/plugins/NerdTools/include/Settings.php', {action: 'token', github_token: value,
            csrf_token: $('#package_form input[name="csrf_token"]').val()})
          .done(function() { packageError(''); $('#github_token').val(''); packageQuery(false); })
          .fail(function(xhr) { packageError(requestError(xhr)); })
          .always(function() { $('#btnToken, #btnTokenRemove').prop('disabled', false); });
    }
    $('#btnToken').on('click', function() { saveToken($('#github_token').val()); });
    $('#btnTokenRemove').on('click', function() { saveToken(''); });
    packageQuery(false);
});
function packageError(message) {
    $('#package_error').text(message || '').prop('hidden', !message);
}
function requestError(xhr) {
    return xhr.responseJSON && xhr.responseJSON.error ? xhr.responseJSON.error : 'NerdTools request failed; please try again.';
}
function planData(action) {
    var data = {
        action: action,
        update_check_hours: $('#update_check_hours').val(),
        csrf_token: $('#package_form input[name="csrf_token"]').val()
    };
    $('.pkgaction').each(function() { data['actions[' + $(this).attr('data-package') + ']'] = this.value; });
    return data;
}
function isCatalogResponseCurrent(revision) {
    return revision === catalogRevision;
}
function isConfirmationCurrent(revision, id) {
    return revision === planRevision && nerdPlan !== null && nerdPlan.id === id;
}
function isDisplayedPlanCurrent() {
    return displayedPlanRevision === planRevision && nerdPlan !== null && displayedPlanId === nerdPlan.id;
}
function canCancelConfirmation() {
    return confirmingPlanId === null;
}
function packageQuery(force) {
    if (force) {
        $('#btnRefresh').prop('disabled', true);
        $.post('/plugins/NerdTools/include/Settings.php', {
            action: 'refresh', csrf_token: $('#package_form input[name="csrf_token"]').val()
        }).done(function() { packageError(''); packageQuery(false); })
          .fail(function(xhr) { packageError(requestError(xhr)); packageQuery(false); })
          .always(function() { $('#btnRefresh').prop('disabled', false); });
        return;
    }
    var revision = ++catalogRevision;
    nerdLoaded = false; nerdPlan = null; planRevision++;
    $('#btnApply').prop('disabled', true).val('Apply changes');
    $('#catalog_status').text('Retrieving package information…');
    $.getJSON('/plugins/NerdTools/include/PackageQuery.php').done(function(data) {
        if (!isCatalogResponseCurrent(revision)) return;
        var body = $('#tblPackages tbody').empty();
        $.each(data.packages, function(_, pkg) {
            var row = $('<tr>').attr('data-package', pkg.name);
            var select = $('<select>', {'class': 'pkgaction', 'data-package': pkg.name, 'aria-label': 'Action for ' + pkg.tool});
            $.each(pkg.actions, function(_, action) { select.append($('<option>').val(action).text(actionLabels[action])); });
            select.val(pkg.default_action).prop('disabled', pkg.actions.length === 1).on('change', requestPlan);
            row.append($('<td>').append(select));
            row.append($('<td>', {'class': 'package wrapper'}).text(pkg.tool));
            row.append($('<td>').text((pkg.dependency_labels || pkg.dependencies).join(', ') || '—'));
            row.append($('<td>', {'class': 'version-value'}).text(pkg.installed_version || (pkg.installed ? 'Unknown' : '—')));
            row.append($('<td>', {'class': 'version-value'}).text(pkg.available_version || '—'));
            row.append($('<td>', {'class': 'planned-change'}).text('Planning…'));
            row.append($('<td>').text(pkg.size));
            body.append(row);
        });
        if (!data.packages.length) {
            body.append($('<tr>').append($('<td>', {colspan: 7}).text('No compatible catalog cached. Select Check for updates to retrieve the package list.')));
        }
        var hours = String(data.hours);
        if (!$('#update_check_hours option').filter(function() { return this.value === hours; }).length) {
            $('#update_check_hours').append($('<option>').val(hours).text('Every ' + hours + ' hours'));
        }
        $('#update_check_hours').val(hours);
        var releases = $('#package_release').empty().append($('<option>').val('latest').text('Latest compatible release'));
        $.each(data.releases || [], function(_, tag) { releases.append($('<option>').val(tag).text(tag)); });
        releases.val(data.selector || 'latest');
        $('#github_token').attr('placeholder', data.token_set ? 'Saved (hidden)' : 'Not set');
        var state = data.state || {};
        var statusText = data.release ? 'Package release: ' + data.release + '. ' : '';
        statusText += state.last_success ? 'Last successful check: ' + new Date(state.last_success * 1000).toLocaleString() + '. ' : 'No successful catalog check yet. ';
        if (state.error) statusText += 'Last check failed: ' + state.error + '. Using the last-good cache. ';
        if (data.hours === 0) statusText += 'Automatic checks are disabled.';
        else if (state.next_check) statusText += 'Next eligible check: ' + new Date(state.next_check * 1000).toLocaleString() + '.';
        $('#catalog_status').text(statusText);
        var offers = $('#external_offers div').empty();
        $.each(data.external_packages || [], function(_, offer) {
            offers.append($('<p>').append(
                $('<strong>').text(offer.name + ' ' + offer.version + ': '),
                document.createTextNode(offer.license_notice + ' '),
                $('<a>', {href: offer.license_url, target: '_blank', rel: 'noopener noreferrer'}).text('Read license terms'),
                document.createTextNode(' · '),
                $('<a>', {href: offer.url, target: '_blank', rel: 'noopener noreferrer'}).text('Download original from RARLAB')
            ));
        });
        $('#external_offers').prop('hidden', !(data.external_packages || []).length);
        var provided = $('#provided_packages ul').empty();
        $.each(data.provided_packages || [], function(_, capability) {
            provided.append($('<li>').text(capability.name + ' (Unraid baseline ' + capability.version + ')'));
        });
        $('#provided_packages').prop('hidden', !(data.provided_packages || []).length);
        nerdLoaded = true;
        $('#tblPackages').trigger('update').trigger('search', [$('#tblPackages')[0].config.lastSearch]);
        requestPlan();
    }).fail(function(xhr) {
        if (!isCatalogResponseCurrent(revision)) return;
        packageError(requestError(xhr));
        $('#catalog_status').text('Package information is unavailable. Existing settings have not been changed.');
        $('#btnApply').prop('disabled', true);
    });
}
function requestPlan() {
    if (!nerdLoaded) return;
    planRevision++; nerdPlan = null;
    $('#btnApply').prop('disabled', true).val('Planning changes…');
    if (planRequest) { planQueued = true; return; }
    runPlanRequest(planRevision);
}
function runPlanRequest(revision) {
    planQueued = false;
    planRequest = $.post('/plugins/NerdTools/include/Settings.php', planData('plan')).done(function(plan) {
        if (revision !== planRevision) return;
        nerdPlan = plan; renderPlan(plan); packageError('');
    }).fail(function(xhr) {
        if (revision !== planRevision) return;
        nerdPlan = null; packageError(requestError(xhr));
        $('#btnApply').prop('disabled', true).val('Apply changes');
    }).always(function() {
        planRequest = null;
        if (nerdLoaded && (planQueued || revision !== planRevision)) runPlanRequest(planRevision);
        else planQueued = false;
    });
}
function renderPlan(plan) {
    $('#tblPackages tbody tr').removeClass('package-changed package-update-available');
    $.each(plan.rows, function(name, state) {
        var row = $('#tblPackages tbody tr').filter(function() { return $(this).attr('data-package') === name; });
        var label = changeLabels[state.change] || state.change;
        if (state.automatic) label = 'Required ' + label.toLowerCase();
        if (state.reason) label += ' — ' + state.reason;
        row.find('.planned-change').text(label);
        if ($.inArray(state.change, ['install','upgrade','downgrade','remove']) !== -1) row.addClass('package-changed');
        else if (state.change === 'update-available' || state.change === 'downgrade-available') row.addClass('package-update-available');
    });
    var count = plan.changes.length;
    $('#btnApply').val(count ? 'Apply ' + count + (count === 1 ? ' change' : ' changes') : 'No changes')
        .prop('disabled', count === 0);
    $('#tblPackages').trigger('update');
}
function Apply() {
    if (!nerdLoaded || !nerdPlan) { packageError('Wait for the package plan to finish.'); return; }
    if (!nerdPlan.changes.length) return;
    displayedPlanId = nerdPlan.id; displayedPlanRevision = planRevision;
    var body = $('#package_plan_table tbody').empty();
    $.each(nerdPlan.changes, function(_, change) {
        var from = change.from || '—'; var to = change.to || '—';
        body.append($('<tr>').append(
            $('<td>').text(changeLabels[change.change] || change.change),
            $('<td>').text(change.package || 'Automatic catalog checks'),
            $('<td>', {'class': 'version-value'}).text(from + ' → ' + to),
            $('<td>').text(change.reason || (change.automatic ? 'Required dependency action' : 'Requested'))
        ));
    });
    $('#package_plan_summary').text(nerdPlan.changes.length + (nerdPlan.changes.length === 1 ? ' action will run.' : ' actions will run.'));
    $('#package_plan_error').text('').prop('hidden', true);
    document.getElementById('package_plan_dialog').showModal();
    document.getElementById('btnPlanCancel').focus();
}
function confirmPlan() {
    if (!isDisplayedPlanCurrent()) {
        document.getElementById('package_plan_dialog').close();
        packageError('Package state changed. Review the refreshed plan before confirming.');
        return;
    }
    var revision = displayedPlanRevision; var planId = displayedPlanId;
    var data = planData('confirm'); data.plan = planId; confirmingPlanId = planId;
    $('#btnPlanCancel, #btnPlanConfirm').prop('disabled', true);
    $.post('/plugins/NerdTools/include/Settings.php', data).done(function(response) {
        if (!isConfirmationCurrent(revision, planId) || response.plan !== planId) {
            document.getElementById('package_plan_dialog').close();
            packageError('Package state changed. Review the refreshed plan before confirming.');
            nerdPlan = null; requestPlan(); return;
        }
        packageError('');
        document.getElementById('package_plan_dialog').close();
        openBox('/plugins/NerdTools/scripts/packagemanager&arg1=--download&arg2=--uninstall&arg3='
            + encodeURIComponent('--plan=' + response.plan), 'Package Manager', 600, 900, true);
    }).fail(function(xhr) {
        document.getElementById('package_plan_dialog').close();
        packageError(requestError(xhr) + ' Review the refreshed plan before confirming.');
        nerdPlan = null; requestPlan();
    }).always(function() {
        if (confirmingPlanId === planId) confirmingPlanId = null;
        $('#btnPlanCancel, #btnPlanConfirm').prop('disabled', false);
    });
}
