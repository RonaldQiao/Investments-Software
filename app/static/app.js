const slider=document.querySelector("#leverage");
if(slider){const output=document.querySelector("#leverage-value");const show=()=>output.textContent=`${Number(slider.value).toFixed(1)}×`;slider.addEventListener("input",show);show();}
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
