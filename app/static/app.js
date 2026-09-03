const slider=document.querySelector("#leverage");
if(slider){const output=document.querySelector("#leverage-value");const show=()=>output.textContent=`${Number(slider.value).toFixed(1)}×`;slider.addEventListener("input",show);show();}
