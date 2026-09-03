const slider=document.querySelector("#leverage");
if(slider){const output=document.querySelector("#leverage-value");const show=()=>output.textContent=`${Number(slider.value).toFixed(1)}×`;slider.addEventListener("input",show);show();}
const addInstrument=document.querySelector('form.form-grid[action="/instruments"]');
if(addInstrument){
  const symbol=addInstrument.elements.symbol;
  const name=addInstrument.elements.name;
  const assetClass=addInstrument.elements.asset_class;
  const currency=addInstrument.elements.currency;
  const yahooSymbol=addInstrument.elements.yahoo_symbol;
  const avgPrice=addInstrument.elements.avg_price;
  const status=document.querySelector("#lookup-status");
  [assetClass,currency].forEach(field=>field.addEventListener("input",()=>{field.dataset.touched="1";}));
  let lastLookup="";
  const lookup=async()=>{
    const value=symbol.value.trim().toUpperCase();
    if(!value||value===lastLookup)return;
    lastLookup=value;
    status.textContent="";
    try{
      const response=await fetch(`/api/lookup?symbol=${encodeURIComponent(value)}`);
      if(response.status===404){status.textContent="not on Yahoo";return;}
      if(!response.ok)return;
      const meta=await response.json();
      if(!name.value)name.value=meta.name||"";
      if(!assetClass.dataset.touched)assetClass.value=meta.asset_class;
      if(!currency.dataset.touched)currency.value=meta.currency;
      if(!yahooSymbol.value)yahooSymbol.value=meta.symbol;
      if(meta.price!=null){
        const price=Number(meta.price);
        avgPrice.placeholder=price.toFixed(2);
        status.textContent=`Yahoo · ${price.toFixed(2)} ${meta.currency}`;
      }
    }catch(error){status.textContent="";}
  };
  symbol.addEventListener("change",lookup);
  symbol.addEventListener("blur",lookup);
}
document.querySelectorAll(".form-grid,.update-form").forEach(form=>{
  const select=form.querySelector('select[name="asset_class"]');
  const details=form.querySelector(".contract-fields");
  if(!select||!details)return;
  const toggle=()=>{details.hidden=!["option","future"].includes(select.value);};
  select.addEventListener("change",toggle);toggle();
});
document.querySelectorAll(".edit-form").forEach(form=>{
  const select=form.querySelector('select[name="asset_class"]');
  if(!select)return;
  const names=["underlying","expiry","strike","option_type"];
  const toggle=()=>{
    names.forEach(name=>{
      const field=form.elements[name];
      if(field)field.hidden=!["option","future"].includes(select.value);
    });
  };
  select.addEventListener("change",toggle);toggle();
});
