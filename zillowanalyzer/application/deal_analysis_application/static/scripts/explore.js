import { ListingsRetriever } from './util/listingsRetriever.js';

function formDataRetriever() {
    return {
        region: document.getElementById("region").value,
        home_type: document.getElementById("home_type").value,
        year_built: document.getElementById("year_built").value,
        max_price: document.getElementById("max_price").value,
        city: document.getElementById("city").value,
        is_waterfront: document.getElementById("is_waterfront").checked,
        is_cashflowing: document.getElementById("is_cashflowing").checked,
        num_properties_per_page: document.getElementById("num_properties_per_page").value,
    };
}

document.addEventListener('DOMContentLoaded', function() {
    const elems = document.querySelectorAll('select');
    M.FormSelect.init(elems, {}); // Assuming you're using Materialize for form elements

    const listingsRetriever = new ListingsRetriever(formDataRetriever, '/explore');
    listingsRetriever.initPageEvents();
});
