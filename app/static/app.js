const slider=document.querySelector("#leverage");
if(slider){const output=document.querySelector("#leverage-value");const show=()=>output.textContent=`${Number(slider.value).toFixed(1)}×`;slider.addEventListener("input",show);show();}
const fundSwitch=document.querySelector(".fund-switch select");
if(fundSwitch)fundSwitch.addEventListener("change",()=>fundSwitch.form.submit());
const addInstrument=document.querySelector('form.form-grid[action="/instruments"]');
if(addInstrument){
  const symbol=addInstrument.elements.symbol;
  const name=addInstrument.elements.name;
  const assetClass=addInstrument.elements.asset_class;
  const currency=addInstrument.elements.currency;
  const currencyOther=addInstrument.elements.currency_other;
  const yahooSymbol=addInstrument.elements.yahoo_symbol;
  const avgPrice=addInstrument.elements.avg_price;
  const fxRate=addInstrument.elements.fx_rate;
  const status=document.querySelector("#lookup-status");
  [assetClass,currency].forEach(field=>field.addEventListener("input",()=>{field.dataset.touched="1";}));
  let lastLookup="";
  let autoYahooSymbol=null;
  let autoPricePlaceholder=null;
  const resetLookup=()=>{
    if(autoYahooSymbol!==null&&yahooSymbol.value===autoYahooSymbol)yahooSymbol.value="";
    if(autoPricePlaceholder!==null&&avgPrice.placeholder===autoPricePlaceholder)avgPrice.placeholder="";
    autoYahooSymbol=null;
    autoPricePlaceholder=null;
    lastLookup="";
  };
  const lookup=async()=>{
    const value=symbol.value.trim().toUpperCase();
    if(!value||value===lastLookup)return;
    lastLookup=value;
    status.textContent="";
    try{
      const response=await fetch(`/api/lookup?symbol=${encodeURIComponent(value)}`);
      if(response.status===404){resetLookup();status.textContent="not on Yahoo";return;}
      if(!response.ok){resetLookup();return;}
      const meta=await response.json();
      if(!name.value)name.value=meta.name||"";
      if(!assetClass.dataset.touched)assetClass.value=meta.asset_class;
      if(!currency.dataset.touched){
        const option=[...currency.options].find(item=>item.value===meta.currency);
        currency.value=option?meta.currency:"other";
        if(currency.value==="other"&&currencyOther)currencyOther.value=meta.currency;
      }
      if(fxRate&&!fxRate.value&&meta.fx_rate!=null)fxRate.value=meta.fx_rate;
      if(!yahooSymbol.value){yahooSymbol.value=meta.symbol;autoYahooSymbol=meta.symbol;}
      if(meta.price!=null){
        const price=Number(meta.price);
        avgPrice.placeholder=price.toFixed(2);
        autoPricePlaceholder=avgPrice.placeholder;
        status.textContent=`Yahoo · ${price.toFixed(2)} ${meta.currency}`;
      }
    }catch(error){resetLookup();status.textContent="";}
  };
  symbol.addEventListener("change",lookup);
  symbol.addEventListener("blur",lookup);
}
document.querySelectorAll(".currency-select").forEach(select=>{
  const other=select.parentElement.querySelector(".currency-other");
  if(!other)return;
  const toggle=()=>{
    other.hidden=select.value!=="other";
    if(select.value!=="other")other.value="";
  };
  select.addEventListener("change",toggle);toggle();
});
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
