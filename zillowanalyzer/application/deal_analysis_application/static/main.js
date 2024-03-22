document.addEventListener('DOMContentLoaded', function() {
    var elems = document.querySelectorAll('select');
    M.FormSelect.init(elems, {});
    var elems = document.querySelectorAll('.sidenav');
    M.Sidenav.init(elems);
    // Initialize other components that are common across pages here
})
