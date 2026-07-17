// Lógica de filtro compartilhada entre resultado.html e verificar_conciliados.html.
// Alterna a visibilidade de itens com base em um atributo data-* booleano ("1"/"0").
function filtrarPorMarcacao(selector, dataAttr, filtro, filtroVerdadeiro) {
  document.querySelectorAll(selector).forEach(function (el) {
    var marcado = el.dataset[dataAttr] === '1';
    var visivel = filtro === 'todas' || (filtro === filtroVerdadeiro ? marcado : !marcado);
    el.style.display = visivel ? '' : 'none';
  });
}
