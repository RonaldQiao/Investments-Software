const slider=document.querySelector("#leverage");
if(slider){const output=document.querySelector("#leverage-value");const show=()=>output.textContent=`${Number(slider.value).toFixed(1)}×`;slider.addEventListener("input",show);show();}
async function refreshPrices(event){event.preventDefault();const button=event.target.querySelector("button");button.disabled=true;const response=await fetch("/api/prices/refresh",{method:"POST"});if(response.ok)location.reload();else{button.disabled=false;alert("Price refresh failed");}return false;}
