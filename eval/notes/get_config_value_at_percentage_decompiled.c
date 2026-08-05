

undefined4 FUN_00109d5a(undefined8 param_1,int param_2)



{

  char cVar1;

  int iVar2;

  undefined4 *puVar3;

  undefined4 uVar4;

  int local_44;

  undefined1 local_38 [28];

  int local_1c;

  

  cVar1 = FUN_0010a92e(param_1);

  if (cVar1 == '\0') {

    local_44 = param_2;

    if (param_2 < 0) {

      local_44 = 0;

    }

    if (100 < local_44) {

      local_44 = 100;

    }

    FUN_00109b2c(local_38,param_1);

    iVar2 = FUN_00103bde(local_38);

    local_1c = (iVar2 * local_44 + 99) / 100;

    if (local_1c < 1) {

      local_1c = 1;

    }

    puVar3 = (undefined4 *)FUN_00103f14(local_38,(long)(local_1c + -1));

    uVar4 = *puVar3;

    FUN_00103336(local_38);

  }

  else {

    uVar4 = 0;

  }

  return uVar4;

}



