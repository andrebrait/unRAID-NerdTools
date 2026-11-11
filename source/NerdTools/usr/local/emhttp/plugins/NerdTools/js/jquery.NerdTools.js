var nerdPackages = {};
var nerdLoaded = false;
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
            filter_functions: {'.filter-version': true, '.filter-installed': true}
        }
    });
    $('#package_form').on('submit', function(event) { event.preventDefault(); Apply(); });
    $('#btnApply').on('click', Apply);
    $('#btnRefresh').on('click', function() { packageQuery(true); });
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
    $('#catalog_status').text('Retrieving package information…');
    $.getJSON('/plugins/NerdTools/include/PackageQuery.php').done(function(data) {
        nerdPackages = {};
        var body = $('#tblPackages tbody').empty();
        $.each(data.packages, function(_, pkg) {
            nerdPackages[pkg.name] = pkg;
            var row = $('<tr>');
            var checkbox = $('<input>', {type: 'checkbox', 'class': 'pkgcheckbox', 'aria-label': 'Install ' + pkg.tool})
                .attr('data-package', pkg.name).prop('checked', pkg.selected).prop('disabled', pkg.external && !pkg.selected);
            var value = $('<input>', {type: 'hidden', 'class': 'pkgvalue', name: 'packages[' + pkg.name + ']'})
                .attr('data-package', pkg.name).val(pkg.selected ? 'yes' : 'no');
            row.append($('<td>').append(checkbox, value));
            row.append($('<td>', {'class': 'package wrapper'}).text(pkg.tool + ' ' + pkg.version));
            row.append($('<td>').text((pkg.dependency_labels || pkg.dependencies).join(', ')));
            var status = $('<td>');
            if (pkg.external) status.text('Managed by Unraid or another installer (protected)');
            else if (pkg.unavailable) status.text('Absent from this release; switch Off to remove');
            else if (pkg.exact) status.text('Up-to-date');
            else if (pkg.installed) {
                status.append($('<button>', {type: 'button', 'class': 'update'}).text('Update ready').on('click', function() {
                    checkbox.prop('checked', true).trigger('change'); Apply();
                }));
            } else status.text('Uninstalled');
            row.append(status, $('<td>').text(pkg.installed ? 'yes' : 'no'), $('<td>').text(pkg.size), $('<td>').text(pkg.current));
            body.append(row);
        });
        if (!data.packages.length) body.append($('<tr>').append($('<td>', {colspan: 7}).text('No compatible catalog cached. Select Force Updates to retrieve the package list.')));
        $('.pkgcheckbox').switchButton({labels_placement: 'right', on_label: 'On', off_label: 'Off'}).on('change', function() {
            syncPreferences(); checkDepends();
        });
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
        $('#btnApply').prop('disabled', !data.packages.length);
        $('#tblPackages').trigger('update').trigger('search', [$('#tblPackages')[0].config.lastSearch]);
    }).fail(function(xhr) {
        packageError(requestError(xhr));
        $('#catalog_status').text('Package information is unavailable. Existing settings have not been changed.');
        $('#btnApply').prop('disabled', true);
    });
}
function syncPreferences() {
    $('.pkgcheckbox').each(function() {
        $(this).closest('tr').find('.pkgvalue').val(this.checked ? 'yes' : 'no');
    });
}
function checkDepends() {
    var selected = {};
    function visit(name) {
        if (selected[name] || !nerdPackages[name]) return;
        selected[name] = true;
        $.each(nerdPackages[name].dependencies, function(_, dep) { visit(dep); });
    }
    $('.pkgcheckbox:checked').each(function() { visit($(this).attr('data-package')); });
    $('.pkgcheckbox').each(function() {
        if (selected[$(this).attr('data-package')] && !this.checked) {
            $(this).switchButton({checked: true}); this.checked = true;
        }
    });
    syncPreferences();
}
function Apply() {
    if (!nerdLoaded) { packageError('Load the catalog before applying changes.'); return; }
    checkDepends();
    $('#btnApply').prop('disabled', true);
    $.post('/plugins/NerdTools/include/Settings.php', $('#package_form').serialize()).done(function() {
        packageError('');
        openBox('/plugins/NerdTools/scripts/packagemanager&arg1=--download&arg2=--uninstall', 'Package Manager', 600, 900, true);
    }).fail(function(xhr) { packageError(requestError(xhr)); })
      .always(function() { $('#btnApply').prop('disabled', false); });
}
